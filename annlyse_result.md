# SWE-bench evaluation log

- Time: `2026-05-11T20:24:38`
- Command:

```bash
/home/hangli22/miniforge3/envs/sweagent/bin/python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Lite --split test --predictions_path results/retrieval_lihang_11/preds.json --max_workers 1 --run_id retrieval_lihang_11_20_30
```

## Output

```text
<frozen runpy>:128: RuntimeWarning: 'swebench.harness.run_evaluation' found in sys.modules after import of package 'swebench.harness', but prior to execution of 'swebench.harness.run_evaluation'; this may result in unpredictable behaviour
Using the latest cached version of the dataset since princeton-nlp/SWE-bench_Lite couldn't be found on the Hugging Face Hub (offline mode is enabled).
2026-05-11 20:24:44,833 - datasets.load - WARNING - Using the latest cached version of the dataset since princeton-nlp/SWE-bench_Lite couldn't be found on the Hugging Face Hub (offline mode is enabled).
Found the latest cached dataset configuration 'default' at /home/hangli22/.cache/huggingface/datasets/princeton-nlp___swe-bench_lite/default/0.0.0/6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2 (last modified on Wed Apr 29 04:50:52 2026).
2026-05-11 20:24:44,835 - datasets.packaged_modules.cache.cache - WARNING - Found the latest cached dataset configuration 'default' at /home/hangli22/.cache/huggingface/datasets/princeton-nlp___swe-bench_lite/default/0.0.0/6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2 (last modified on Wed Apr 29 04:50:52 2026).
Using the latest cached version of the dataset since princeton-nlp/SWE-bench_Lite couldn't be found on the Hugging Face Hub (offline mode is enabled).
2026-05-11 20:24:45,400 - datasets.load - WARNING - Using the latest cached version of the dataset since princeton-nlp/SWE-bench_Lite couldn't be found on the Hugging Face Hub (offline mode is enabled).
Found the latest cached dataset configuration 'default' at /home/hangli22/.cache/huggingface/datasets/princeton-nlp___swe-bench_lite/default/0.0.0/6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2 (last modified on Wed Apr 29 04:50:52 2026).
2026-05-11 20:24:45,401 - datasets.packaged_modules.cache.cache - WARNING - Found the latest cached dataset configuration 'default' at /home/hangli22/.cache/huggingface/datasets/princeton-nlp___swe-bench_lite/default/0.0.0/6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2 (last modified on Wed Apr 29 04:50:52 2026).
Running 7 instances...

Evaluation:   0%|          | 0/7 [00:00<?, ?it/s, error=0, ✓=0, ✖=0]
Evaluation:   0%|          | 0/7 [06:06<?, ?it/s, ✓=0, ✖=1, error=0]
Evaluation:  14%|█▍        | 1/7 [06:07<36:42, 367.01s/it, ✓=0, ✖=1, error=0]
Evaluation:  14%|█▍        | 1/7 [11:29<36:42, 367.01s/it, ✓=1, ✖=1, error=0]
Evaluation:  29%|██▊       | 2/7 [11:29<28:23, 340.62s/it, ✓=1, ✖=1, error=0]
Evaluation:  29%|██▊       | 2/7 [17:09<28:23, 340.62s/it, ✓=1, ✖=2, error=0]
Evaluation:  43%|████▎     | 3/7 [17:09<22:41, 340.37s/it, ✓=1, ✖=2, error=0]
Evaluation:  43%|████▎     | 3/7 [23:15<22:41, 340.37s/it, ✓=1, ✖=3, error=0]
Evaluation:  57%|█████▋    | 4/7 [23:15<17:32, 350.78s/it, ✓=1, ✖=3, error=0]
Evaluation:  57%|█████▋    | 4/7 [28:48<17:32, 350.78s/it, ✓=1, ✖=4, error=0]
Evaluation:  71%|███████▏  | 5/7 [28:48<11:28, 344.22s/it, ✓=1, ✖=4, error=0]
Evaluation:  71%|███████▏  | 5/7 [34:02<11:28, 344.22s/it, ✓=2, ✖=4, error=0]
Evaluation:  86%|████████▌ | 6/7 [34:02<05:34, 334.01s/it, ✓=2, ✖=4, error=0]
Evaluation:  86%|████████▌ | 6/7 [39:00<05:34, 334.01s/it, ✓=3, ✖=4, error=0]
Evaluation: 100%|██████████| 7/7 [39:00<00:00, 322.26s/it, ✓=3, ✖=4, error=0]
Evaluation: 100%|██████████| 7/7 [39:00<00:00, 334.41s/it, ✓=3, ✖=4, error=0]
All instances run.
Cleaning cached images...
Removed 0 images.
Total instances: 300
Instances submitted: 10
Instances completed: 7
Instances incomplete: 290
Instances resolved: 3
Instances unresolved: 4
Instances with empty patches: 3
Instances with errors: 0
Unstopped containers: 0
Unremoved images: 0
Report written to openai__deepseek-v4-flash.retrieval_lihang_11_20_30.json

```

## Exit status

```text
0
```


# Per-instance evaluation summary

- Time: `2026-05-11T21:04:03`
- preds: `results/retrieval_lihang_11/preds.json`
- run_id: `retrieval_lihang_11_20_30`
- parsed_json_files: `7`

## Parsed harness JSON files

- `logs/run_evaluation/retrieval_lihang_11_20_30/openai__deepseek-v4-flash/django__django-11630/report.json`
- `logs/run_evaluation/retrieval_lihang_11_20_30/openai__deepseek-v4-flash/django__django-11815/report.json`
- `logs/run_evaluation/retrieval_lihang_11_20_30/openai__deepseek-v4-flash/django__django-11848/report.json`
- `logs/run_evaluation/retrieval_lihang_11_20_30/openai__deepseek-v4-flash/django__django-11905/report.json`
- `logs/run_evaluation/retrieval_lihang_11_20_30/openai__deepseek-v4-flash/django__django-11910/report.json`
- `logs/run_evaluation/retrieval_lihang_11_20_30/openai__deepseek-v4-flash/django__django-11964/report.json`
- `logs/run_evaluation/retrieval_lihang_11_20_30/openai__deepseek-v4-flash/django__django-11999/report.json`

## Summary

```text
patch_status: {'NON_EMPTY': 7, 'EMPTY_PATCH': 3}
eval_status : {'FAILED': 4, 'EMPTY_PATCH': 3, 'RESOLVED': 3}
```

## Per-instance table

| instance_id | patch_status | eval_status |
|---|---:|---:|
| `django__django-11630` | NON_EMPTY | FAILED |
| `django__django-11742` | EMPTY_PATCH | EMPTY_PATCH |
| `django__django-11797` | EMPTY_PATCH | EMPTY_PATCH |
| `django__django-11815` | NON_EMPTY | RESOLVED |
| `django__django-11848` | NON_EMPTY | FAILED |
| `django__django-11905` | NON_EMPTY | FAILED |
| `django__django-11910` | NON_EMPTY | FAILED |
| `django__django-11964` | NON_EMPTY | RESOLVED |
| `django__django-11999` | NON_EMPTY | RESOLVED |
| `django__django-12113` | EMPTY_PATCH | EMPTY_PATCH |


CSV also written to: `results/retrieval_lihang_11/eval_summary.csv`
