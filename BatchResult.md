# swebench lite
## deepseek v3:
### Retrieval_mode:
#### 0:19 前十九个18个非空，6个正确
    Running 18 instances...
    Evaluation: 100%|████████████████████████████████████████████████| 18/18 [2:05:24<00:00, 351.79s/it, ✓=6, ✖=12, error=0]All instances run.
    Evaluation: 100%|████████████████████████████████████████████████| 18/18 [2:05:24<00:00, 418.01s/it, ✓=6, ✖=12, error=0]
    Cleaning cached images...
    Removed 0 images.
    Total instances: 300
    Instances submitted: 19
    Instances completed: 18
    Instances incomplete: 281
    Instances resolved: 6
    Instances unresolved: 12
    Instances with empty patches: 1
    Instances with errors: 0
    Unstopped containers: 0
    Unremoved images: 0
    Report written to openai__deepseek-v3:671b.retrieval_0_11.json

### Baseline mode:
#### 0：20：前20个
    prompt修改前：仅有一个ModelPatch非空，也就是仅有一个实际修改
    
    prompt修改后：
    14:18:19 [INFO] 进度: 20/40 | 有patch: 5 | Submitted: 5
    14:18:19 [INFO] 摘要已保存：./results/baseline/run_summary.json
    ============================================================
    批量评测完成 | mode=baseline
    总数:         20
    Submitted:    5 (25.0%)
    EmptyPatch:   1 (5.0%)
    有 patch:     5 (25.0%)
    LimitsExceed: 14
    ============================================================
    注意：Submitted 率和有patch率不等于实际解决率。
        请用 sb-cli 提交 preds.json 获取真实 resolve_rate。

    Running 5 instances...
    Evaluation: 100%|██████████████████████████████████████████████████| 5/5 [1:24:38<00:00, 1022.68s/it, ✓=2, ✖=3, error=0]All instances run.
    Evaluation: 100%|██████████████████████████████████████████████████| 5/5 [1:24:38<00:00, 1015.76s/it, ✓=2, ✖=3, error=0]
    Cleaning cached images...
    Removed 0 images.
    Total instances: 300
    Instances submitted: 20
    Instances completed: 5
    Instances incomplete: 280
    Instances resolved: 2
    Instances unresolved: 3
    Instances with empty patches: 15
    Instances with errors: 0
    Unstopped containers: 0
    Unremoved images: 0
    Report written to openai__deepseek-v3:671b.retrieval_0_11.json

## deepseek v4 flash:
### Retrieval_mode:

### baseline_mode: