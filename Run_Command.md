python mini_swe_agent_integration/run_swebench_batch.py \
  --mode baseline \
  --model_name openai/deepseek-v4-flash \
  --api_base https://uni-api.cstcloud.cn/v1 \
  --subset lite \
  --split test \
  --slice 20:30 \
  --output_dir ./results/baseline_docker \
  --repos_dir ./repos \
  --cache_dir ./cache \
  --workers 1 \
  --step_limit 60 \
  --use_docker \
  --docker_image sweagent-multipy:latest \
  --redo

python mini_swe_agent_integration/run_swebench_batch.py \
  --mode retrieval \
  --model_name openai/deepseek-v4-flash \
  --api_base https://uni-api.cstcloud.cn/v1 \
  --subset lite \
  --split test \
  --slice 20:21 \
  --output_dir ./results/retrieval_zhou_09 \
  --repos_dir ./repos \
  --cache_dir ./cache \
  --workers 1 \
  --step_limit 60 \
  --use_docker \
  --docker_image sweagent-multipy:latest \
  --redo

以上为每次运行时的指令，可调整的参数：
--mode为使用的模式，baseline为基准模式，retrieval为检索模式
--slice后面为运行的实例编号，20:30表示第20个到第29个
model有deepseek-v3:671b，deepseek-v4-flash

python -m swebench.harness.run_evaluation   --dataset_name princeton-nlp/SWE-bench_Lite   --split test   --predictions_path ./preds.json   --max_workers 1   --run_id retrieval_0_11

以上是评测preds的指令，注意路径名字是否正确

使用docker运行完发现repos文件夹没有权限删除：
sudo chown -R $(whoami):$(whoami) repos
将权限改为当前用户，然后再删除
ls -ld repos  
确认属主


git log --pretty=format:"%H  %an  %ad  %s" --date=short -20
得到各次commit的编号
git fetch origin
git switch main
git reset --hard <commit_id>
切换过去

跑 SWE-bench harness 评测的必要条件：
能运行：
python -m swebench.harness.run_evaluation --help
如果不能运行，需要在当前 Python/conda 环境里安装 SWE-bench

