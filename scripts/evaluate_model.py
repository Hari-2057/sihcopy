"""Evaluate a trained OceanEmbed checkpoint on test and val splits.

Computes RMSE, MAE, bias, correlation across all standard depths, and saves
a summary report and training progress curve.
"""

from __future__ import annotations

import argparse
import json
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
def evaluate_split(model, loader, device, depth_std: np.ndarray) -> dict:
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
    out = {
        "loss": total_loss / max(n, 1),
        "rmse_c": (sums["rmse"] / max(n, 1) * scale).cpu().numpy(),
        "bias_c": (sums["bias"] / max(n, 1) * scale).cpu().numpy(),
        "corr": (sums["corr"] / max(n, 1)).cpu().numpy(),
    }
    return out


def plot_training_curves(history_path: Path, output_path: Path):
    if not history_path.exists():
        return
    history = json.loads(history_path.read_text())
    if not history:
        return

    epochs = [h["epoch"] for h in history]
    train_loss = [h["train_loss"] for h in history]
    val_loss = [h["val_loss"] for h in history]
    val_rmse = [h.get("val_rmse_c_mean", 0) for h in history]
    val_corr = [h.get("val_corr_mean", 0) for h in history]

    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5), dpi=150)
    fig.patch.set_facecolor("#0b0f19")

    for ax in axes:
        ax.set_facecolor("#111827")
        ax.grid(True, linestyle="--", alpha=0.25, color="#9ca3af")
        ax.tick_params(colors="#9ca3af")
        for spine in ax.spines.values():
            spine.set_color("#374151")

    # Loss
    axes[0].plot(epochs, train_loss, label="Train Loss", color="#38bdf8", lw=2)
    axes[0].plot(epochs, val_loss, label="Val Loss", color="#f43f5e", lw=2)
    axes[0].set_title("Loss", color="#f3f4f6", fontsize=13, fontweight="bold")
    axes[0].set_xlabel("Epoch", color="#9ca3af")
    axes[0].legend(facecolor="#1f2937", edgecolor="#374151", labelcolor="#f3f4f6")

    # RMSE
    axes[1].plot(epochs, val_rmse, color="#34d399", lw=2)
    axes[1].set_title("Validation RMSE (°C)", color="#f3f4f6", fontsize=13, fontweight="bold")
    axes[1].set_xlabel("Epoch", color="#9ca3af")

    # Correlation
    axes[2].plot(epochs, val_corr, color="#fbbf24", lw=2)
    axes[2].set_title("Validation Correlation", color="#f3f4f6", fontsize=13, fontweight="bold")
    axes[2].set_xlabel("Epoch", color="#9ca3af")

    plt.tight_layout()
    plt.savefig(output_path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close()
    print(f"Saved training curve to {output_path}")


def main():
    args = parse_args()
    run_dir = ROOT / args.run_dir
    export_dir = ROOT / args.export_dir

    ckpt_path = run_dir / "best.pt"
    if not ckpt_path.exists():
        print(f"No checkpoint found at {ckpt_path}", file=sys.stderr)
        return 1

    device_name = args.device
    if not device_name:
        device_name = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
    device = torch.device(device_name)
    print(f"Using device: {device}")

    ckpt = torch.load(ckpt_path, map_location=device, weights_only=False)
    cfg_dict = ckpt.get("config", {})

    # Load test and val datasets
    val_ds = OceanEmbedDataset(export_dir, "val", patch=None)
    test_ds = OceanEmbedDataset(export_dir, "test", patch=None)

    val_loader = DataLoader(val_ds, batch_size=1, shuffle=False, num_workers=0)
    test_loader = DataLoader(test_ds, batch_size=1, shuffle=False, num_workers=0)

    model = OceanEmbedNet(
        in_channels=val_ds.in_channels,
        out_depths=len(val_ds.depths),
        width=cfg_dict.get("width", 48),
        attn_blocks=cfg_dict.get("attn_blocks", 2),
        heads=cfg_dict.get("heads", 4),
    ).to(device)

    model.load_state_dict(ckpt["model"])

    depth_std = val_ds.depth_std()

    print("\nEvaluating on Validation split...")
    val_results = evaluate_split(model, val_loader, device, depth_std)

    print("Evaluating on Test split (2008-2009 unseen years)...")
    test_results = evaluate_split(model, test_loader, device, depth_std)

    out_metrics = {
        "best_epoch": ckpt.get("epoch", 0),
        "val": {
            "loss": float(val_results["loss"]),
            "mean_rmse_c": float(np.mean(val_results["rmse_c"])),
            "mean_corr": float(np.mean(val_results["corr"])),
            "per_depth_rmse": [round(float(x), 4) for x in val_results["rmse_c"]],
            "per_depth_corr": [round(float(x), 4) for x in val_results["corr"]],
        },
        "test": {
            "loss": float(test_results["loss"]),
            "mean_rmse_c": float(np.mean(test_results["rmse_c"])),
            "mean_corr": float(np.mean(test_results["corr"])),
            "per_depth_rmse": [round(float(x), 4) for x in test_results["rmse_c"]],
            "per_depth_corr": [round(float(x), 4) for x in test_results["corr"]],
        },
        "depths": val_ds.depths,
    }

    metrics_file = run_dir / "evaluation_metrics.json"
    metrics_file.write_text(json.dumps(out_metrics, indent=2))
    print(f"Saved metrics to {metrics_file}")

    # Plot training curves
    plot_path = ROOT / "reports" / "training_curves.png"
    plot_training_curves(run_dir / "history.json", plot_path)

    print("\n" + "=" * 65)
    print(f"  OCEANEMBED-NET TEST RESULTS (Best Epoch {ckpt.get('epoch', 0)})")
    print("=" * 65)
    print(f"  Test Loss:        {out_metrics['test']['loss']:.4f}")
    print(f"  Test Mean RMSE:   {out_metrics['test']['mean_rmse_c']:.3f} °C")
    print(f"  Test Mean Corr:   {out_metrics['test']['mean_corr']:.3f}")
    print("-" * 65)
    print(f"  {'Depth (m)':>10} | {'RMSE (°C)':>12} | {'Correlation':>12}")
    print("-" * 65)
    for d, r, c in zip(val_ds.depths, out_metrics["test"]["per_depth_rmse"], out_metrics["test"]["per_depth_corr"]):
        print(f"  {d:>10.0f} | {r:>12.3f} | {c:>12.3f}")
    print("=" * 65)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
