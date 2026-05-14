# SWE-bench evaluation log

- Time: `2026-05-14T10:56:55`
- Command:

```bash
/home/hangli22/miniforge3/envs/sweagent/bin/python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Verified --split test --predictions_path results/test_verified_0_1/preds.json --max_workers 1 --run_id test_verified_0_1
```

## Output

```text
[run_and_analyse] pid=28577
[run_and_analyse] subset=verified
[run_and_analyse] dataset_name=princeton-nlp/SWE-bench_Verified
[run_and_analyse] split=test
[run_and_analyse] eval_lock=/tmp/swebench_eval.lock
[run_and_analyse] started_eval_step_at=2026-05-14T10:56:55
[run_and_analyse] acquired_eval_lock_at=2026-05-14T10:56:55
<frozen runpy>:128: RuntimeWarning: 'swebench.harness.run_evaluation' found in sys.modules after import of package 'swebench.harness', but prior to execution of 'swebench.harness.run_evaluation'; this may result in unpredictable behaviour
Using the latest cached version of the dataset since princeton-nlp/SWE-bench_Verified couldn't be found on the Hugging Face Hub (offline mode is enabled).
2026-05-14 10:56:58,214 - datasets.load - WARNING - Using the latest cached version of the dataset since princeton-nlp/SWE-bench_Verified couldn't be found on the Hugging Face Hub (offline mode is enabled).
Found the latest cached dataset configuration 'default' at /home/hangli22/CodeAgent/hf_cache/datasets/princeton-nlp___swe-bench_verified/default/0.0.0/c104f840cc67f8b6eec6f759ebc8b2693d585d4a (last modified on Tue May 12 23:12:03 2026).
2026-05-14 10:56:58,214 - datasets.packaged_modules.cache.cache - WARNING - Found the latest cached dataset configuration 'default' at /home/hangli22/CodeAgent/hf_cache/datasets/princeton-nlp___swe-bench_verified/default/0.0.0/c104f840cc67f8b6eec6f759ebc8b2693d585d4a (last modified on Tue May 12 23:12:03 2026).
Using the latest cached version of the dataset since princeton-nlp/SWE-bench_Verified couldn't be found on the Hugging Face Hub (offline mode is enabled).
2026-05-14 10:56:58,534 - datasets.load - WARNING - Using the latest cached version of the dataset since princeton-nlp/SWE-bench_Verified couldn't be found on the Hugging Face Hub (offline mode is enabled).
Found the latest cached dataset configuration 'default' at /home/hangli22/CodeAgent/hf_cache/datasets/princeton-nlp___swe-bench_verified/default/0.0.0/c104f840cc67f8b6eec6f759ebc8b2693d585d4a (last modified on Tue May 12 23:12:03 2026).
2026-05-14 10:56:58,535 - datasets.packaged_modules.cache.cache - WARNING - Found the latest cached dataset configuration 'default' at /home/hangli22/CodeAgent/hf_cache/datasets/princeton-nlp___swe-bench_verified/default/0.0.0/c104f840cc67f8b6eec6f759ebc8b2693d585d4a (last modified on Tue May 12 23:12:03 2026).
Running 1 instances...

Evaluation:   0%|          | 0/1 [00:00<?, ?it/s, error=0, ✓=0, ✖=0]
Evaluation:   0%|          | 0/1 [06:17<?, ?it/s, ✓=1, ✖=0, error=0]
Evaluation: 100%|██████████| 1/1 [06:17<00:00, 377.38s/it, ✓=1, ✖=0, error=0]
Evaluation: 100%|██████████| 1/1 [06:17<00:00, 377.42s/it, ✓=1, ✖=0, error=0]
All instances run.
Cleaning cached images...
Removed 0 images.
Total instances: 500
Instances submitted: 1
Instances completed: 1
Instances incomplete: 499
Instances resolved: 1
Instances unresolved: 0
Instances with empty patches: 0
Instances with errors: 0
Unstopped containers: 0
Unremoved images: 0
Report written to openai__deepseek-v4-flash.test_verified_0_1.json

```

## Exit status

```text
0
```


# Per-instance evaluation summary

- Time: `2026-05-14T11:03:35`
- preds: `results/test_verified_0_1/preds.json`
- run_id: `test_verified_0_1`
- parsed_json_files: `1`

## Parsed harness JSON files

- `logs/run_evaluation/test_verified_0_1/openai__deepseek-v4-flash/astropy__astropy-12907/report.json`

## Summary

```text
patch_status: {'NON_EMPTY': 1}
eval_status : {'RESOLVED': 1}
```

## Per-instance table

| instance_id | patch_status | eval_status |
|---|---:|---:|
| `astropy__astropy-12907` | NON_EMPTY | RESOLVED |


CSV also written to: `results/test_verified_0_1/eval_summary.csv`
