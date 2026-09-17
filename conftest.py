"""pytest 集成：暴露 eval_harness fixture（N11 · 测试框架集成）。

任何依赖本 harness 的项目，把此文件或 ``pytest_plugins = ["eval_harness.plugin"]`` 接入，
即可在 pytest 用例里用 ``eval_harness.run(...)`` / ``eval_harness.assert_pass_rate(...)``。
"""
pytest_plugins = ["eval_harness.plugin"]
