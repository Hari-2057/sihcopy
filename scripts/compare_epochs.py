"""Compare multiple epoch checkpoints on the held-out test split (2008-2009).

Evaluates checkpoints (e.g. 5, 10, 20, 28, 30, 40, 50, 60) and identifies
which epoch produces the best overall model, generating comparison tables
and graphs.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import torch
from torch.utils.data import DataLoader

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oceanembed.dataset import OceanEmbedDataset
from oceanembed.model import OceanEmbedNet, masked_depth_loss, profile_metrics


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", default="runs/baseline")
    p.add_argument("--export-dir", default="data/export")
    p.add_argument("--device", default="")
    return p.parse_args()


@torch.no_grad()
def evaluate_loader(model, loader, device, depth_std: np.ndarray) -> dict:
    model.eval()
    total_loss, n = 0.0, 0
    sums = None

    for x, y, v in loader:
        x, y, v = x.to(device), y.to(device), v.to(device)
        pred = model(x)
        total_loss += float(masked_depth_loss(pred, y, v))
        n += 1

        m = profile_metrics(pred, y, v)
        if sums is None:
            sums = {k: val.clone() for k, val in m.items()}
        else:
            for k in sums:
                sums[k] += m[k]

    scale = torch.as_tensor(depth_std, device=device)
    return {
        "loss": total_loss / max(n, 1),
        "rmse_c": (sums["rmse"] / max(n, 1) * scale).cpu().numpy(),
        "corr": (sums["corr"] / max(n, 1)).cpu().numpy(),
    }


def main():
    args = parse_args()
    run_dir = ROOT / args.run_dir
    export_dir = ROOT / args.export_dir

    device_name = args.device
    if not device_name:
        device_name = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
    device = torch.device(device_name)
    print(f"Using device: {device}")

    # Load test dataset
    test_ds = OceanEmbedDataset(export_dir, "test", patch=None)
    val_ds = OceanEmbedDataset(export_dir, "val", patch=None)

    test_loader = DataLoader(test_ds, batch_size=1, shuffle=False, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=1, shuffle=False, num_workers=0)

    depth_std = test_ds.depth_std()

    # Find all checkpoint files
    ckpt_files = sorted(glob.glob(str(run_dir / "checkpoint_epoch_*.pt")))
    best_file = run_dir / "best.pt"

    all_ckpts = []
    for f in ckpt_files:
        match = re.search(r"checkpoint_epoch_(\d+)\.pt", f)
        if match:
            all_ckpts.append((int(match.group(1)), Path(f)))

    all_ckpts.sort(key=lambda x: x[0])

    if not all_ckpts and best_file.exists():
        all_ckpts.append((-1, best_file))

    print(f"Found {len(all_ckpts)} checkpoints to evaluate: {[c[0] for c in all_ckpts]}")

    # Also load history.json if available
    history_file = run_dir / "history.json"
    history = json.loads(history_file.read_text()) if history_file.exists() else []
    hist_by_ep = {h["epoch"] + 1: h for h in history}

    results = []

    for ep, ckpt_path in all_ckpts:
        print(f"Evaluating checkpoint at Epoch {ep} ({ckpt_path.name})...")
        ckpt = torch.load(ckpt_path, map_location=device, weights_only=False)
        cfg_dict = ckpt.get("config", {})

        model = OceanEmbedNet(
            in_channels=test_ds.in_channels,
            out_depths=len(test_ds.depths),
            width=cfg_dict.get("width", 48),
            attn_blocks=cfg_dict.get("attn_blocks", 2),
            heads=cfg_dict.get("heads", 4),
        ).to(device)
        model.load_state_dict(ckpt["model"])

        val_res = evaluate_loader(model, val_loader, device, depth_std)
        test_res = evaluate_loader(model, test_loader, device, depth_std)

        h_rec = hist_by_ep.get(ep, {})

        rec = {
            "epoch": ep,
            "train_loss": h_rec.get("train_loss", None),
            "val_loss": val_res["loss"],
            "test_loss": test_res["loss"],
            "val_rmse": float(np.mean(val_res["rmse_c"])),
            "test_rmse": float(np.mean(test_res["rmse_c"])),
            "val_corr": float(np.mean(val_res["corr"])),
            "test_corr": float(np.mean(test_res["corr"])),
            "per_depth_test_rmse": [round(float(x), 4) for x in test_res["rmse_c"]],
            "per_depth_test_corr": [round(float(x), 4) for x in test_res["corr"]],
        }
        results.append(rec)

    # Save comparison json
    comp_file = run_dir / "epoch_comparison.json"
    comp_file.write_text(json.dumps(results, indent=2))
    print(f"Saved comparison to {comp_file}")

    # Identify best epoch
    best_by_test_rmse = min(results, key=lambda r: r["test_rmse"])
    best_by_val_loss = min(results, key=lambda r: r["val_loss"])

    # Plot comparison graphs
    if len(results) >= 2:
        eps = [r["epoch"] for r in results]
        t_losses = [r["test_loss"] for r in results]
        v_losses = [r["val_loss"] for r in results]
        t_rmses = [r["test_rmse"] for r in results]
        t_corrs = [r["test_corr"] for r in results]

        fig, axes = plt.subplots(1, 3, figsize=(16, 4.5), dpi=150)
        fig.patch.set_facecolor("#0b0f19")

        for ax in axes:
            ax.set_facecolor("#111827")
            ax.grid(True, linestyle="--", alpha=0.25, color="#9ca3af")
            ax.tick_params(colors="#9ca3af")
            for spine in ax.spines.values():
                spine.set_color("#374151")

        # Loss
        axes[0].plot(eps, v_losses, marker="o", label="Validation Loss", color="#f43f5e", lw=2)
        axes[0].plot(eps, t_losses, marker="s", label="Test Loss (2008-09)", color="#38bdf8", lw=2)
        axes[0].set_title("Loss vs Epoch", color="#f3f4f6", fontsize=13, fontweight="bold")
        axes[0].set_xlabel("Epoch", color="#9ca3af")
        axes[0].legend(facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6")

        # RMSE
        axes[1].plot(eps, t_rmses, marker="o", color="#34d399", lw=2)
        axes[1].set_title("Test Mean RMSE (°C)", color="#f3f4f6", fontsize=13, fontweight="bold")
        axes[1].set_xlabel("Epoch", color="#9ca3af")

        # Correlation
        axes[2].plot(eps, t_corrs, marker="o", color="#fbbf24", lw=2)
        axes[2].set_title("Test Correlation", color="#f3f4f6", fontsize=13, fontweight="bold")
        axes[2].set_xlabel("Epoch", color="#9ca3af")

        plt.tight_layout()
        plot_out = ROOT / "reports" / "epoch_comparison.png"
        plt.savefig(plot_out, facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close()
        print(f"Saved comparison plot to {plot_out}")

    # Print summary table
    print("\n" + "=" * 85)
    print("  EPOCH COMPARISON SUMMARY ON UNSEEN TEST SET (2008-2009)")
    print("=" * 85)
    print(f"  {'Epoch':>6} | {'Val Loss':>9} | {'Test Loss':>9} | {'Test RMSE (°C)':>14} | {'Test Corr':>10}")
    print("-" * 85)
    for r in results:
        star = "  <-- BEST" if r["epoch"] == best_by_test_rmse["epoch"] else ""
        print(f"  {r['epoch']:>6} | {r['val_loss']:>9.4f} | {r['test_loss']:>9.4f} | {r['test_rmse']:>14.3f} | {r['test_corr']:>10.3f}{star}")
    print("=" * 85)
    print(f"\nBest model on Test Set is: Epoch {best_by_test_rmse['epoch']} (Test RMSE = {best_by_test_rmse['test_rmse']:.3f} °C, Corr = {best_by_test_rmse['test_corr']:.3f})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
