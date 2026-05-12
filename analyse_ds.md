# SWE-bench evaluation log

- Time: `2026-05-12T12:03:45`
- Command:

```bash
/home/hangli22/miniforge3/envs/sweagent/bin/python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Lite --split test --predictions_path results/smoke_ds_0_1/preds.json --max_workers 1 --run_id smoke_ds_0_1_0_1
```

## Output

```text
<frozen runpy>:128: RuntimeWarning: 'swebench.harness.run_evaluation' found in sys.modules after import of package 'swebench.harness', but prior to execution of 'swebench.harness.run_evaluation'; this may result in unpredictable behaviour
Using the latest cached version of the dataset since princeton-nlp/SWE-bench_Lite couldn't be found on the Hugging Face Hub (offline mode is enabled).
2026-05-12 12:03:48,378 - datasets.load - WARNING - Using the latest cached version of the dataset since princeton-nlp/SWE-bench_Lite couldn't be found on the Hugging Face Hub (offline mode is enabled).
Found the latest cached dataset configuration 'default' at /home/hangli22/.cache/huggingface/datasets/princeton-nlp___swe-bench_lite/default/0.0.0/6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2 (last modified on Wed Apr 29 04:50:52 2026).
2026-05-12 12:03:48,379 - datasets.packaged_modules.cache.cache - WARNING - Found the latest cached dataset configuration 'default' at /home/hangli22/.cache/huggingface/datasets/princeton-nlp___swe-bench_lite/default/0.0.0/6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2 (last modified on Wed Apr 29 04:50:52 2026).
Using the latest cached version of the dataset since princeton-nlp/SWE-bench_Lite couldn't be found on the Hugging Face Hub (offline mode is enabled).
2026-05-12 12:03:49,678 - datasets.load - WARNING - Using the latest cached version of the dataset since princeton-nlp/SWE-bench_Lite couldn't be found on the Hugging Face Hub (offline mode is enabled).
Found the latest cached dataset configuration 'default' at /home/hangli22/.cache/huggingface/datasets/princeton-nlp___swe-bench_lite/default/0.0.0/6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2 (last modified on Wed Apr 29 04:50:52 2026).
2026-05-12 12:03:49,680 - datasets.packaged_modules.cache.cache - WARNING - Found the latest cached dataset configuration 'default' at /home/hangli22/.cache/huggingface/datasets/princeton-nlp___swe-bench_lite/default/0.0.0/6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2 (last modified on Wed Apr 29 04:50:52 2026).
Running 1 instances...

Evaluation:   0%|          | 0/1 [00:00<?, ?it/s, error=0, ✓=0, ✖=0]
Evaluation:   0%|          | 0/1 [05:52<?, ?it/s, ✓=1, ✖=0, error=0]
Evaluation: 100%|██████████| 1/1 [05:52<00:00, 352.60s/it, ✓=1, ✖=0, error=0]
Evaluation: 100%|██████████| 1/1 [05:52<00:00, 352.62s/it, ✓=1, ✖=0, error=0]
All instances run.
Cleaning cached images...
Removed 0 images.
Total instances: 300
Instances submitted: 1
Instances completed: 1
Instances incomplete: 299
Instances resolved: 1
Instances unresolved: 0
Instances with empty patches: 0
Instances with errors: 0
Unstopped containers: 0
Unremoved images: 0
Report written to openai__deepseek-v4-flash.smoke_ds_0_1_0_1.json

```

## Exit status

```text
0
```


# Per-instance evaluation summary

- Time: `2026-05-12T12:09:46`
- preds: `results/smoke_ds_0_1/preds.json`
- run_id: `smoke_ds_0_1_0_1`
- parsed_json_files: `1`

## Parsed harness JSON files

- `logs/run_evaluation/smoke_ds_0_1_0_1/openai__deepseek-v4-flash/astropy__astropy-12907/report.json`

## Summary

```text
patch_status: {'NON_EMPTY': 1}
eval_status : {'RESOLVED': 1}
```

## Per-instance table

| instance_id | patch_status | eval_status |
|---|---:|---:|
| `astropy__astropy-12907` | NON_EMPTY | RESOLVED |


CSV also written to: `results/smoke_ds_0_1/eval_summary.csv`
