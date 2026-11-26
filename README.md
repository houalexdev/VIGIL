# VIGIL · 工业设备异常数据智能监测

面向工业设备预测性维护（PHM）的时序异常检测工作台。纯 Python 实现：**NiceGUI 前端 + 27 个自研检测算法 + Parquet 数据落盘 + 本地大模型生成判据**，深色科技风界面。

```
┌─ 规则中心 ────────────────┐   ┌─ 异常检测 ─────────────────────┐
│ 算法库（27 内置 + 自定义） │   │ 分组场景：选场景 → 时间窗 → 出结果 │
│ 规则库（测点 + 多算法组合）│   │ 自定义检测：测点 → 数据 → AI 规则 │
│ 分组场景（规则打包）       │   │ 每条规则一页：折线图 + 异常点表    │
└──────────────────────────┘   └───────────────────────────────┘
```

适用对象：CNC 主轴、电机、泵、压缩机、风机、变频器、PLC 产线等带多通道时序测点的工业设备。
温度、振动、电流、压力、位域状态字、计数回绕、枚举故障码、越限与趋势等异常形态均有对应算法。

## 快速开始

```bash
pip install -r requirements.txt
python main.py                       # http://localhost:8080
# 或
VIGIL_PORT=8000 python -m app.main
```

首次启动会自动完成：建库（`data/vigil.db`）→ 加载 `algorithm/` 下 27 个算法 →
读取 `config/*.yaml` 写入预置场景 **「旋转机械」** 与 **「电气与PLC」**（共 22 条工业判据）→ 异步探测本地大模型。

> 环境变量优先于 YAML：`VIGIL_PORT`、`VIGIL_HOST`、`VIGIL_DATA`、`VIGIL_DB`、
> `VIGIL_LLM_URL/KEY/MODEL/TIMEOUT`、`VIGIL_CONFIG_DIR`（整体替换配置目录）、
> `VIGIL_CONFIG`（指向另一份 app.yaml）。

## 配置：全部在 `config/*.yaml`，代码里不写死

所有业务参数、判据、提示词、配色都在 YAML 里，改配置不需要动 Python。
界面上「规则中心 → 运行配置」可实时查看生效值与文件来源，并有「重载配置」按钮（不重启即可生效）。

| 文件 | 管什么 |
|---|---|
| `config/app.yaml` | 应用标识、服务端口、路径、大模型连接、告警等级与配色、值域形态、时间窗、模拟参数（含「需外部数据」算法清单）、Parquet 落盘格式、界面渲染（抽稀点数、表格列） |
| `config/telemetry.yaml` | 工业测点目录：34 个测点的值域 / 单位 / 位宽 / LSB / 设备部位；未登记测点的兜底规则 |
| `config/presets.yaml` | 预置规则与分组场景（旋转机械 11 条 + 电气与PLC 11 条） |
| `config/prompts.yaml` | 大模型系统提示词、用户提示词模板、期望规则条数、请求默认参数 |
| `config/theme.yaml` | 配色变量、CSS、Logo（等级色标由 `app.yaml → levels` 自动生成） |

**这些是被代码使用的，不是文档**：改 `presets.yaml` 里的阈值就是改判据，改 `levels` 里的颜色就是改整站配色，
改 `prompts.yaml` 就是改 AI 生成规则的口径。

值域覆盖 5 种形态，与工业现场一一对应：

| 值域 | 工业测点举例 |
|---|---|
| 连续量 | 主轴前后轴承温度、电机绕组温度、振动速度、电流、液压压力、冷却液温度 |
| 枚举量 | 运行模式、变频器状态、负载状态、诊断码 |
| 计数型 | 累计运行小时、启停循环、故障累计、报警累计（含回绕） |
| 位域型 | PLC 输入状态字、诊断标志字、报警位字 |
| 标志型 | 主轴启停、急停、安全联锁、加热器使能 |

加载规则（`app/yamls.py`）：

- 相对路径 → 相对项目根；`${data}/parquet` 这类引用会先解析；
- 环境变量覆盖 YAML；
- YAML 语法错 / 结构错（规则缺字段、场景引用不存在的规则、测点重复）→ **启动即失败**，不会带着坏配置跑出错误判据；
- 文件缺失 → 用内置最小默认值兜底并在启动日志与「运行配置」页给出告警。

## 目录结构

```
algorithm/            27 个检测算法 + base.py 统一模板（算法唯一依赖）
config/               全部配置（见上表）
app/
  main.py             入口：建库、加载算法、写预置、注册页面
  yamls.py            YAML 加载层：读取 / 校验 / 环境变量覆盖 / ${} 路径解析
  config.py           YAML → 常量的翻译层（业务代码只从这里取配置）
  meta.py             测点元数据目录（数据源 telemetry.yaml）
  store.py            SQLite（规则/场景/数据集索引）+ Parquet 读写
  custom_algo.py      自定义算法：校验 → 执行 → 注册进算法注册表
  simulate.py         模拟数据（按规则注入异常 + 真实算法回测闭环）
  engine.py           检测引擎：规则 → 算法 → 异常点 → 前端视图模型
  llm.py              本地大模型生成规则（含校验与启发式兜底）
  presets.py          预置规则与场景装配（数据源 presets.yaml）
  report.py           报告层：异常点/规则结果 → 表格行、JSON、CSV（无 UI 依赖，网页与命令行共用）
  cli.py              命令行：覆盖全部功能，不启动 WebUI
  theme.py            深色科技风样式（数据源 theme.yaml）
  ui/                 common（页面骨架/参数表单/图表）/ rules_center / detection
data/parquet/<代号>/<YYYYMMDD>/<起始ms>_<结束ms>.parquet
user_algorithms/      自定义算法源码
tests/                无头交互测试 + 配置层测试 + 命令行测试（72 项）
vigil                 可执行入口（等价 python -m app.cli，自动挑解释器）
```

## 规则中心

**算法库**：卡片墙展示 27 个算法（编号/名称/层级/类别/命中类型/可用值域/参数数），可看参数详情；
新增算法在弹窗里写一个继承 `BaseAlgorithm` 的类，保存时做「语法 → 结构 → 合成数据试跑」三级校验，
通过即注册（可被规则直接引用），支持更新与删除。自定义算法落盘为 `user_algorithms/*.py`，重启仍在。

**规则库**：一条规则 = 一个测点代号 + 若干算法（每个算法独立配参）。参数表单由算法的 `PARAM_SPECS`
自动生成（枚举/布尔/数值/列表/JSON），带类型、范围、单位、条件显隐与帮助提示，默认值即推荐值。
规则来源区分预置/用户/AI。

**分组场景**：把若干规则打包成一个可复用场景，场景决定检测与分页顺序。

## 异常检测

### 分组场景（批量）

选场景 → 填开始/结束时间戳（毫秒或 `YYYY-MM-DD HH:MM:SS`，带 1h/4h/12h/24h 快捷）→ **模拟数据**。
系统按场景内每条规则的算法与参数生成数据并注入异常，再用**真实算法回测**，不足 5 处就加大剂量重来
（最多 4 轮）。结果按规则分页：折线图（异常点按等级着色 + 等级标签钉 + 分类图例）+ 异常点表格
（时间/等级/命中类型/算法/触发值/阈值/持续时长/定位维度/证据摘要），并展示各算法执行状态。

### 自定义检测（单测点 + AI 生成判据）

1. 在「测点代号」文本框输入代号（自由输入，不带下拉候选）+ 时间窗；
2. **读取规则 / 保存规则**（01 卡片内，不另建列表）：
   - 「读取规则」按当前代号查库，把结果**刷新到 02 卡片下方的「检测规则」列表**（只显示该代号的规则）；
   - 「保存规则」把 02「检测规则」列表里当前展示的规则**写回数据库**；
   - 改代号会丢掉已读取的快照，避免把旧代号的规则存到新代号下；列表为空时保存会提示无需保存。
3. **模拟数据**：生成干净序列、落盘 Parquet、给出数据画像（min/max/mean/σ/分位数/单拍差分）。
4. **AI 生成规则**：把数据画像 + 27 个算法可配置项（含 supported_kinds、参数范围、条件依赖）交给
   本地大模型，由它选算法、配阈值，返回 JSON。输出逐条过 `validate_params`：算法不存在/非检测器/
   值域不适配/参数越界的一律剔除并说明原因；模型不可达时自动退化为本地统计口径规则（分位数越界、
   p99 差分跳变、振荡、冻结、缺口），保证功能可用。

   > **本地模型（OpenAI 兼容接口）**：默认 `http://127.0.0.1:8000/v1`，模型 `qwen2.5:14b`，
   > 均可在 `config/app.yaml → llm` 或环境变量 `VIGIL_LLM_URL/KEY/MODEL` 覆盖，不绑定任何内网地址。
   > Qwen3 系列可在 `prompts.yaml → chat_template_kwargs` 保留 `enable_thinking: false` 关闭思考模式；
   > 换非 Qwen3 模型时删掉该段即可。
5. 生成的规则直接入库，可在 02 列表里勾选/编辑/删除，也可手工新建；
6. **开始分析**：按勾选规则逐条分析，输出与场景模式一致的表 + 图。

### 预设内容

内置两个工业场景，共 **22 条规则 / 2 个分组场景**，逐条映射到 27 个算法。改法见 `config/presets.yaml`。

**「旋转机械」11 条**（`R-SPN-NN`）：

| 规则 | 测点 | 算法 | 说明 |
|---|---|---|---|
| 01 | SPN1001 主轴前轴承温度 | A-01 | 温度越限 |
| 02 | SPN1002 电机绕组温度 | A-01 | 绕组温度越限 |
| 03 | SPN1002 电机绕组温度 | A-05 | 升温速率超限（比越限更早预警） |
| 04 | SPN1003 主轴后轴承温度 | A-03 | 单拍突跳 |
| 05 | SPN1003 主轴后轴承温度 | A-02 | 温度冻结（传感器失效） |
| 06 | VIB3004 振动速度 | A-04 | 噪声能量异常 |
| 07 | VIB3002 振动速度 | A-07 | 振动-电流相关性劣化 |
| 08 | VIB3003 振动速度 | A-09 | 成对通道偏置 |
| 09 | SPN7001 运行模式 | A-24 | 模式驻留过久 |
| 10 | SPN8001 累计运行小时 | A-10 | 计数停滞/回绕 |
| 11 | SPN2001 主轴前轴承温度 | A-27 | 数据缺口 |

**「电气与PLC」11 条**（`R-ELC-NN`）：

| 规则 | 测点 | 算法 | 说明 |
|---|---|---|---|
| 01 | ELC6001 直流母线电压 | A-01 | 电压越限 |
| 02 | ELC6002 A 相电流 | A-01 | 相电流越限 |
| 03 | ELC6003 B 相电流 | A-03 | 电流突跳 |
| 04 | ELC6002 A 相电流 | A-09 | 相电流成对偏置 |
| 05 | ELC7001 变频器状态 | A-23 | 非法枚举码 |
| 06 | ELC8001 故障累计 | A-01 | 故障计数非零 |
| 07 | ELC9002 报警位字 | A-11 | 报警位逐位展开 |
| 08 | ELC9001 PLC 输入状态字 | A-11 | 状态字逐位比对 |
| 09 | ELC0002 诊断标志字 | A-23 | 非法诊断码（致命） |
| 10 | TRM5001 冷却水进口温度 | A-25 | 高温且冷却未开的复合判据 |
| 11 | HYG4001 液压压力 | A-03 | 压力突跳 |

## 关键实现说明

**为什么模拟数据能保证每条规则都有异常**：注入不是"随便加点跳点"，而是每个算法一个专用注入器
（A-07 用整段去相关、A-09 用偏置段+累计分离、A-11 逐位翻转、A-10 冻结/增量、A-27 真删采样点…），
注入完立刻用该算法跑一遍数命中，不够就重试。所以界面上的数字和曲线一定对得上。

**时间与采样**：单序列点数上限 9000，采样周期按时长自适应（1~9.5 s）。上限 9.5 s 是因为 A-03 的
`max_gap_s=10 s` —— 周期再长，跳变算法会自己跳过所有点。

**A-05 的跨度阈值**：速率算法按 11 拍窗口做差分，实际跨度 = `11 × 采样周期`。
若采样周期较大（如长时间窗下 4.8 s），跨度可达 52.8 s，会超过默认 `max_gap_s=10 s` 被缺口保护全部过滤。
预置规则 `R-SPN-03` 已显式设 `max_gap_s: 600`。新增速率类规则时请按 `窗口拍数 × 采样周期` 同步放大该值。

**A-07 的窗口限制**：相关窗长 1800 s 时，2 h 数据最多只能分辨 2 处相关劣化（n/w≈4）。
默认窗口取 12 h 就是为了让这类判据有足够窗长；时长不足时界面会明确写出"A-07 本次仅命中 N 处"，
不会假装满足。

**数据落盘**：`data/parquet/<代号>/<YYYYMMDD>/<起>_<止>.parquet`，`pyarrow` snappy 压缩，
列 `t`(int64 ms) / `v`(float64) / `quality`(uint8)。同一窗口重复模拟覆盖同一文件，元数据在 SQLite 索引。

**"获取数据"按钮**：当前版本不接实时数据采集接口，按钮保留并提示；
接口接入时只需实现 `store.load_series()` 的取数来源，其余链路（规则/算法/绘图/落盘）无需改动。

## 命令行：不启动 WebUI 也能做完所有事

Web 界面能做的事，命令行全部能做。业务逻辑一行都没有复制——`app/cli.py` 只是把
`store / engine / llm / presets` 已有的函数接出来，结果格式化统一走 `app/report.py`，
和网页表格共用同一份转换，所以两边的字段与口径永远一致。

```bash
./vigil --help                # 或用 python -m app.cli
```

根目录的 `vigil` 是个自举脚本：自己找不到依赖时，会按 `VIGIL_PY` → 当前解释器 →
项目 `.venv` → 系统 `python3` 的顺序挑一个装齐依赖的解释器转交过去，所以在任何目录下都能直接跑。
软链到 PATH 即可全局使用：`ln -s "$PWD/vigil" /usr/local/bin/vigil`。

### 命令一览

| 命令组 | 干什么 |
|---|---|
| `algo list / show / template / add / rm / check / src` | 算法库：列表、参数规格、自定义算法模板与三级校验注册 |
| `rule list / show / add / edit / rm / export / import` | 规则库：增删改查、按代号/场景/来源过滤、JSON↔CSV 导出导入 |
| `scene list / show / add / edit / rm` | 分组场景：规则打包与顺序 |
| `preset reload [--force]` | 重新写入预置判据（增量补齐，含 22 条） |
| `detect scene -s <场景>` | 分组场景检测（核心） |
| `detect custom -c <代号> [-r 规则] [-g --save]` | 自定义检测：代号 → 生成规则 → 分析 |
| `llm ping / generate` | 大模型连通性、单独生成规则 |
| `data list / stats / export` | 落盘数据：清单、统计画像、导出 CSV |
| `config show / reload / check` | 配置查看、热重载、YAML 与预置校验 |

### 常用示例

```bash
# 场景检测：最近 12 小时，只看摘要
./vigil detect scene -s S-SPIN -H 12

# 场景检测：连每个异常点明细一起看
./vigil detect scene -s S-ELEC -H 12 --points

# 导出成 CSV（按规则拆文件，或导成一个 JSON 供脚本吃）
./vigil detect scene -s S-SPIN -H 12 -f csv --csv-per-rule -o out/
./vigil detect scene -s S-SPIN -H 12 -f json -o result.json

# 自定义检测：测点下已有规则就直接用
./vigil detect custom -c SPN1001 -H 12

# 自定义检测：没有规则时让模型现场生成，顺手入库
./vigil detect custom -c SPN1004 -H 12 --generate --save

# 离线/无模型时用本地统计口径
./vigil detect custom -c TRM0001 -H 12 --generate --no-llm

# 规则库运维
./vigil rule list --source preset
./vigil rule export --scene S-ELEC -o rules.json
./vigil rule import rules.json

# 换机器前确认环境
./vigil config check
./vigil llm ping
```

### 可脚本化的三个约定

1. **格式**：`-f table`（默认，人读）/ `json`（机器读）/ `csv`（Excel）。
   选 `json` / `csv` 时，过程日志走 stderr，stdout 只有结构化数据，可以直接 `| jq` 或 `| python -c`。
2. **退出码**：`0` 成功 · `1` 业务失败（场景/规则不存在、区间无数据）·
   `2` 参数错误（拼错命令、缺必填）· `3` 依赖不可达（模型离线）· `4` 校验未通过 · `130` 用户中断。
   所以 `./vigil detect scene -s S-SPIN -H 12 || echo "出问题了"` 会按预期工作。
3. **时间**：`--start/--end` 接受毫秒时间戳或 `YYYY-MM-DD HH:MM:SS`；不给就按 `-H/--hours` 取最近 N 小时。

> 「获取数据」按钮对应的采集接口当前版本不接，命令行也一样——`data` 命令读的是本地已落盘的 Parquet。

## 测试

```bash
python -m pytest -q              # 72 项：配置层 / 页面渲染 / 流程 / 编辑器 / 命令行
python -m pytest tests/test_cli.py -q   # 只跑命令行
```

测试不依赖现场大模型服务：流程用例与命令行用例强制走本地启发式路径，只验证「生成 → 入库 → 分析」链路本身。

## 常见配置改动

| 想改什么 | 改哪里 |
|---|---|
| 判据阈值 / 新增预置判据 | `config/presets.yaml`，然后「规则中心 → 重载预置判据」 |
| 测点值域、单位、位宽 | `config/telemetry.yaml` |
| 告警等级名称与配色 | `config/app.yaml → levels`（色标与图表自动跟随） |
| 界面默认时间窗 / 快捷按钮 | `config/app.yaml → windows` |
| 模拟数据点数、采样周期上限、每条规则最少异常数 | `config/app.yaml → simulate` |
| Parquet 目录与命名规则 | `config/app.yaml → storage` |
| AI 生成规则的口径 | `config/prompts.yaml` |
| 整站配色 / CSS | `config/theme.yaml` |
| 大模型地址与密钥 | `config/app.yaml → llm`，或环境变量 `VIGIL_LLM_URL/KEY/MODEL` |
| 关闭/开启模型思考模式 | `config/prompts.yaml → chat_template_kwargs` |

## 扩展一个算法

```python
# user_algorithms/u01_xxx.py（或界面上新建）
from algorithm.base import BaseAlgorithm, ParamSpec, point, HitType, runs
import numpy as np

class MyAlgo(BaseAlgorithm):
    ALGO_ID, ALGO_NAME = "U-01", "我的判据"
    HIT_TYPE = HitType.THRESHOLD
    PARAM_SPECS = [ParamSpec("thr", "阈值", "float", 10.0)]
    def detect(self, ctx, p):
        hit = ctx.series.v > p.thr
        return [point(ctx, p.level, HitType.THRESHOLD, ctx.series.t[a], ctx.series.t[b])
                for a, b in runs(hit)]
```

## 许可证

MIT License — 见 [LICENSE](LICENSE)。
