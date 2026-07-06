# AstrBot 三态竞猜记分插件

这是一个运行在 QQ 群内的轻量娱乐记分小游戏插件，实现方案来自 `QQ 群三态竞猜记分小游戏完整方案.md`。

## 命令

- `/guess start 标题`
- `/guess start 标题 category=apex win=1.8 draw=3 lose=1.8`
- `/guess bet 赢 30`
- `/guess bet 输 all`
- `/guess bet 平 10.5`
- `/guess settle 赢`
- `/guess settle 输`
- `/guess settle 平`
- `/guess cancel`
- `/guess current`
- `/guess score`
- `/guess history`
- `/guess help`

## 规则概要

- 每个群同一时间只能有一个 `open` 或 `closed` 事件。
- 事件默认 5 分钟后停止参与，停止后等待结算或取消。
- 分类支持 `apex` / `other`，大小写不敏感，默认 `other`。
- 结果枚举为 `赢` / `输` / `平`，同时兼容 `win` / `lose` / `draw`。
- 用户首次使用获得 100 分。
- 用户输入积分按四舍五入到整数处理，实际投入至少为 1；`all` 会快速投入当前全部点数。
- 猜中时按 `实际投入 * 倍数` 计算应得，实得再次四舍五入为整数。
- 每天 04:00 懒执行维护：取消未结束事件、退还 pending 记录、把低于 100 的用户补充到 100。
- 不支持分数购买、兑换、转账或线下交易。

## 安装

将 `tri_guess` 文件夹放入 AstrBot 插件目录后重载插件。

## 命令前缀

默认主命令前缀在 `config.py` 中配置：

```python
COMMAND_PREFIX = "guess"
```

如果和其他插件冲突，只需要修改这一处，例如改成 `tri_guess` 后，命令会变为 `/tri_guess help`。

数据文件默认写入插件目录下：

```text
data/tri_guess.sqlite3
```

## 权限说明

所有群成员都可以创建、参与、结算和取消事件，插件内部不做权限控制。需要强管控时，可以在 AstrBot 侧通过权限系统限制对应命令。

群聊中只有明确 @ 机器人的命令才会响应。
