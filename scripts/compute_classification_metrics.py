"""Compute Confusion Matrix, Precision, Recall, and F1-score for OceanEmbed-Net.

Formulates classification benchmarks on the held-out test predictions:
1. 5-Class Ocean Thermal Water Masses (<12C, 12-18C, 18-22C, 22-26C, >26C)
2. 20°C Thermocline Boundary Classification (>= 20°C vs < 20°C)
3. Significant Temperature Anomaly / Marine Heatwave Classification (Anomaly > +1°C)
4. Thermocline-specific (75m-150m) classification
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
from oceanembed.model import OceanEmbedNet


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--ckpt", default="runs/baseline/best.pt")
    p.add_argument("--export-dir", default="data/export")
    p.add_argument("--device", default="")
    p.add_argument("--max-days", type=int, default=0, help="0 for all test days")
    return p.parse_args()


def compute_binary_metrics(y_true: np.ndarray, y_pred: np.ndarray, labels=("Negative", "Positive")):
    tp = int(np.sum((y_true == 1) & (y_pred == 1)))
    fp = int(np.sum((y_true == 0) & (y_pred == 1)))
    fn = int(np.sum((y_true == 1) & (y_pred == 0)))
    tn = int(np.sum((y_true == 0) & (y_pred == 0)))

    prec = tp / max(tp + fp, 1)
    rec = tp / max(tp + fn, 1)
    f1 = 2 * prec * rec / max(prec + rec, 1e-8)
    acc = (tp + tn) / max(tp + fp + fn + tn, 1)

    cm = np.array([[tn, fp], [fn, tp]])
    return {
        "confusion_matrix": cm,
        "accuracy": float(acc),
        "precision": float(prec),
        "recall": float(rec),
        "f1": float(f1),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "labels": list(labels)
    }


def compute_multiclass_metrics(y_true: np.ndarray, y_pred: np.ndarray, class_names: list[str]):
    n_classes = len(class_names)
    cm = np.zeros((n_classes, n_classes), dtype=np.int64)
    for t, p in zip(y_true, y_pred):
        cm[t, p] += 1

    per_class = {}
    f1_list = []
    weights = []
    total_samples = np.sum(cm)

    for i in range(n_classes):
        tp = cm[i, i]
        fp = np.sum(cm[:, i]) - tp
        fn = np.sum(cm[i, :]) - tp
        support = np.sum(cm[i, :])

        prec = float(tp / max(tp + fp, 1))
        rec = float(tp / max(tp + fn, 1))
        f1 = float(2 * prec * rec / max(prec + rec, 1e-8))

        per_class[class_names[i]] = {
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "f1": round(f1, 4),
            "support": int(support),
        }
        f1_list.append(f1)
        weights.append(support)

    macro_f1 = float(np.mean(f1_list))
    weighted_f1 = float(np.sum(np.array(f1_list) * np.array(weights)) / max(total_samples, 1))
    overall_acc = float(np.trace(cm) / max(total_samples, 1))

    return {
        "confusion_matrix": cm,
        "overall_accuracy": round(overall_acc, 4),
        "macro_f1": round(macro_f1, 4),
        "weighted_f1": round(weighted_f1, 4),
        "per_class": per_class,
        "classes": class_names,
    }


def plot_confusion_matrix(cm, classes, title, output_path, cmap="Blues"):
    fig, ax = plt.subplots(figsize=(8, 6.5), dpi=150)
    fig.patch.set_facecolor("#0b0f19")
    ax.set_facecolor("#111827")

    # Normalize by row for visualization
    cm_norm = cm.astype("float") / cm.sum(axis=1)[:, np.newaxis]
    cm_norm = np.nan_to_num(cm_norm)

    im = ax.imshow(cm_norm, interpolation="nearest", cmap=cmap)

    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.ax.tick_params(colors="#9ca3af")
    cbar.set_label("Recall (Normalized)", color="#f3f4f6")

    ax.set_xticks(np.arange(len(classes)))
    ax.set_yticks(np.arange(len(classes)))
    ax.set_xticklabels(classes, rotation=35, ha="right", color="#f3f4f6", fontsize=10)
    ax.set_yticklabels(classes, color="#f3f4f6", fontsize=10)

    for spine in ax.spines.values():
        spine.set_color("#374151")

    thresh = cm_norm.max() / 2.0
    for i in range(len(classes)):
        for j in range(len(classes)):
            val_str = f"{cm[i, j]:,}\n({cm_norm[i, j]:.1%})"
            color = "white" if cm_norm[i, j] > thresh else "black"
            ax.text(j, i, val_str, ha="center", va="center", color=color, fontsize=8.5, fontweight="bold")

    ax.set_title(title, color="#f3f4f6", fontsize=13, fontweight="bold", pad=12)
    ax.set_ylabel("True Thermal Class", color="#38bdf8", fontsize=11, fontweight="bold")
    ax.set_xlabel("Predicted Thermal Class", color="#38bdf8", fontsize=11, fontweight="bold")

    plt.tight_layout()
    plt.savefig(output_path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close()
    print(f"Saved confusion matrix plot to {output_path}")


def main():
    args = parse_args()
    ckpt_path = Path(args.ckpt)
    export_dir = Path(args.export_dir)

    device_name = args.device
    if not device_name:
        device_name = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
    device = torch.device(device_name)
    print(f"Loading checkpoint: {ckpt_path} on {device}")

    # Load stats
    stats_path = export_dir / "norm_stats.json"
    stats = json.loads(stats_path.read_text())
    levels = stats["target"]["levels"]
    depth_means = np.array([lv["mean"] for lv in levels], dtype="float32")
    depth_stds = np.array([lv["std"] for lv in levels], dtype="float32")

    test_ds = OceanEmbedDataset(export_dir, "test", patch=None)
    test_loader = DataLoader(test_ds, batch_size=1, shuffle=False, num_workers=0)

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
    model.eval()

    all_true_c = []
    all_pred_c = []
    all_depth_idx = []
    all_anom_true = []
    all_anom_pred = []

    print("Running inference across test days...")
    with torch.no_grad():
        for day_idx, (x, y, v) in enumerate(test_loader):
            if args.max_days and day_idx >= args.max_days:
                break
            x, y, v = x.to(device), y.to(device), v.to(device)
            pred = model(x)

            # Move to cpu numpy
            y_np = y.squeeze(0).cpu().numpy()     # (15, H, W)
            pred_np = pred.squeeze(0).cpu().numpy()
            v_np = v.squeeze(0).cpu().numpy()

            # Rescale back to degrees Celsius: T = norm * std + mean
            y_c = y_np * depth_stds[:, None, None] + depth_means[:, None, None]
            pred_c = pred_np * depth_stds[:, None, None] + depth_means[:, None, None]

            for d in range(len(test_ds.depths)):
                valid_mask = v_np[d]
                if not np.any(valid_mask):
                    continue
                t_vals = y_c[d][valid_mask]
                p_vals = pred_c[d][valid_mask]

                all_true_c.append(t_vals)
                all_pred_c.append(p_vals)
                all_depth_idx.append(np.full_like(t_vals, d, dtype=np.int32))

                # Standardized anomaly = (T - mean) / std = norm
                all_anom_true.append(y_np[d][valid_mask] * depth_stds[d])
                all_anom_pred.append(pred_np[d][valid_mask] * depth_stds[d])

    true_c = np.concatenate(all_true_c)
    pred_c = np.concatenate(all_pred_c)
    depth_idx = np.concatenate(all_depth_idx)
    anom_true = np.concatenate(all_anom_true)
    anom_pred = np.concatenate(all_anom_pred)

    print(f"Total evaluated ocean points across all depths & test days: {len(true_c):,}")

    # =========================================================================
    # Task 1: 5-Class Thermal Water Mass Classification
    # =========================================================================
    thermal_classes = [
        "Abyssal (<12°C)",
        "Sub-Thermocline (12-18°C)",
        "Lower Thermocline (18-22°C)",
        "Upper Thermocline (22-26°C)",
        "Mixed Layer (>26°C)",
    ]

    def to_thermal_class(t):
        out = np.zeros_like(t, dtype=np.int32)
        out[(t >= 12.0) & (t < 18.0)] = 1
        out[(t >= 18.0) & (t < 22.0)] = 2
        out[(t >= 22.0) & (t < 26.0)] = 3
        out[t >= 26.0] = 4
        return out

    true_class = to_thermal_class(true_c)
    pred_class = to_thermal_class(pred_c)

    res_thermal = compute_multiclass_metrics(true_class, pred_class, thermal_classes)

    # =========================================================================
    # Task 2: 20°C Isotherm / Thermocline Boundary Classification
    # =========================================================================
    bin_true_20 = (true_c >= 20.0).astype(int)
    bin_pred_20 = (pred_c >= 20.0).astype(int)
    res_20c = compute_binary_metrics(bin_true_20, bin_pred_20, labels=["Cold (<20°C)", "Warm (>=20°C)"])

    # =========================================================================
    # Task 3: Marine Heatwave / Significant Warm Anomaly (> +1.0°C)
    # =========================================================================
    mhw_true = (anom_true > 1.0).astype(int)
    mhw_pred = (anom_pred > 1.0).astype(int)
    res_mhw = compute_binary_metrics(mhw_true, mhw_pred, labels=["Normal", "Warm Anomaly (>+1°C)"])

    # =========================================================================
    # Task 4: Thermocline Region Specifically (75m to 150m: indices 6, 7, 8, 9)
    # =========================================================================
    tc_mask = (depth_idx >= 6) & (depth_idx <= 9)
    tc_true_c = true_c[tc_mask]
    tc_pred_c = pred_c[tc_mask]
    tc_true_bin = (tc_true_c >= 20.0).astype(int)
    tc_pred_bin = (tc_pred_c >= 20.0).astype(int)
    res_tc_20c = compute_binary_metrics(tc_true_bin, tc_pred_bin, labels=["Cold (<20°C)", "Warm (>=20°C)"])

    tc_true_class = to_thermal_class(tc_true_c)
    tc_pred_class = to_thermal_class(tc_pred_c)
    res_tc_multi = compute_multiclass_metrics(tc_true_class, tc_pred_class, thermal_classes)

    # Plot Confusion Matrices
    reports_dir = ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    plot_confusion_matrix(
        res_thermal["confusion_matrix"],
        thermal_classes,
        "Ocean Water Mass Thermal Classification (0-1000m)",
        reports_dir / "confusion_matrix_thermal_classes.png",
        cmap="Blues",
    )
    plot_confusion_matrix(
        res_20c["confusion_matrix"],
        res_20c["labels"],
        "20°C Thermocline Boundary Classification",
        reports_dir / "confusion_matrix_20c_boundary.png",
        cmap="Greens",
    )

    # Save complete metrics json
    output_json = ROOT / "runs" / "baseline" / "classification_metrics.json"
    full_output = {
        "thermal_water_masses": {
            "overall_accuracy": res_thermal["overall_accuracy"],
            "macro_f1": res_thermal["macro_f1"],
            "weighted_f1": res_thermal["weighted_f1"],
            "confusion_matrix": res_thermal["confusion_matrix"].tolist(),
            "per_class": res_thermal["per_class"],
        },
        "thermocline_20c_boundary": {
            "accuracy": res_20c["accuracy"],
            "precision": res_20c["precision"],
            "recall": res_20c["recall"],
            "f1": res_20c["f1"],
            "confusion_matrix": res_20c["confusion_matrix"].tolist(),
        },
        "marine_heatwave_anomaly": {
            "accuracy": res_mhw["accuracy"],
            "precision": res_mhw["precision"],
            "recall": res_mhw["recall"],
            "f1": res_mhw["f1"],
            "confusion_matrix": res_mhw["confusion_matrix"].tolist(),
        },
        "thermocline_75_150m_specific": {
            "thermal_mass_macro_f1": res_tc_multi["macro_f1"],
            "thermal_mass_weighted_f1": res_tc_multi["weighted_f1"],
            "boundary_20c_f1": res_tc_20c["f1"],
            "boundary_20c_accuracy": res_tc_20c["accuracy"],
        }
    }
    output_json.write_text(json.dumps(full_output, indent=2))
    print(f"\nSaved metrics to {output_json}")

    # Print summary
    print("\n" + "=" * 75)
    print("  CLASSIFICATION EVALUATION RESULTS (Unseen Test Set 2008-2009)")
    print("=" * 75)
    print(f"\n1. THERMAL WATER MASS CLASSIFICATION (5 Classes, 0-1000m):")
    print(f"   Overall Accuracy: {res_thermal['overall_accuracy']:.2%}")
    print(f"   Macro F1-Score:   {res_thermal['macro_f1']:.4f}")
    print(f"   Weighted F1-Score:{res_thermal['weighted_f1']:.4f}")
    print(f"\n   {'Class':<28} | {'Precision':>9} | {'Recall':>9} | {'F1-Score':>9} | {'Support':>10}")
    print("   " + "-" * 72)
    for c_name, c_data in res_thermal["per_class"].items():
        print(f"   {c_name:<28} | {c_data['precision']:>9.4f} | {c_data['recall']:>9.4f} | {c_data['f1']:>9.4f} | {c_data['support']:>10,}")

    print(f"\n2. 20°C THERMOCLINE BOUNDARY (>= 20°C vs < 20°C):")
    print(f"   Accuracy:  {res_20c['accuracy']:.2%}")
    print(f"   Precision: {res_20c['precision']:.4f}")
    print(f"   Recall:    {res_20c['recall']:.4f}")
    print(f"   F1-Score:  {res_20c['f1']:.4f}")

    print(f"\n3. MARINE HEATWAVE / WARM ANOMALY (> +1.0°C):")
    print(f"   Accuracy:  {res_mhw['accuracy']:.2%}")
    print(f"   Precision: {res_mhw['precision']:.4f}")
    print(f"   Recall:    {res_mhw['recall']:.4f}")
    print(f"   F1-Score:  {res_mhw['f1']:.4f}")

    print(f"\n4. THERMOCLINE REGION SPECIFICALLY (75m - 150m):")
    print(f"   20°C Boundary F1-Score: {res_tc_20c['f1']:.4f} (Accuracy: {res_tc_20c['accuracy']:.2%})")
    print(f"   Water Mass Weighted F1: {res_tc_multi['weighted_f1']:.4f}")
    print("=" * 75)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
