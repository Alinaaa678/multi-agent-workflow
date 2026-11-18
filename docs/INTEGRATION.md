# integration 集成运行说明

`integration` 是项目的跨工作块联调分支，当前包含 B02 讨论控制器和 B06 质量共识 Agent。项目总体说明仍见根目录 [`README.md`](../README.md)。

## B02 验证

```powershell
cd b02-workflow-agent
python -m pytest -q
```

B02 的前端与 WebSocket 使用方式见 [`b02-workflow-agent/README.md`](../b02-workflow-agent/README.md) 和 [`b02-workflow-agent/WEBSOCKET_API.md`](../b02-workflow-agent/WEBSOCKET_API.md)。

## B06 验证

```powershell
cd b06-quality-consensus
python -m pytest -q
python -m tests.smoke
```

B06 的模块说明、证据边界和受控 runner 使用方式见 [`b06-quality-consensus/README.md`](../b06-quality-consensus/README.md)。

## 集成规则

先分别通过 B02 和 B06 的工作块测试，再在本分支运行跨模块检查。集成分支只用于联调，不替代 `main` 的稳定发布；通过 Pull Request 合并到 `main` 前应保留测试命令和结果。
