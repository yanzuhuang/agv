# 仓库与线体 Modbus TCP 控制脚本

`warehouse_modbus.py` 是仓库 PLC 的 Modbus TCP 客户端，支持仓库入库、仓库出库、线体入货、线体出货，以及两个连贯的入出库联动模式。每次运行选择一个模式，完成或终止后关闭 TCP 连接。

仓库操作固定使用 **A 库**，用户可指定 **1～6 列、1～3 层** 的任意有效库位。脚本控制仓库 PLC，不发送外部车辆控制指令。

## 文件与环境

| 文件 | 用途 |
| --- | --- |
| [warehouse_modbus.py](warehouse_modbus.py) | 主入口：参数输入、操作选择、单步和联动调度 |
| [warehouse_plc.py](warehouse_plc.py) | PLC 通信、寄存器读写、状态等待、命令清零与 TCP 关闭 |
| [warehouse_task.py](warehouse_task.py) | 兼容入口，使用同一套流程和参数 |
| [test_warehouse_task.py](test_warehouse_task.py) | 使用模拟 PLC 的离线测试 |
| [requirements.txt](requirements.txt) | 项目依赖清单 |

项目现有 `avg_control` 环境使用 Python 3.10，脚本通信依赖 `pymodbus==3.11.4`。以下示例均在项目根目录的 PowerShell 中执行。

已有环境可直接运行；如果该环境缺少依赖，可安装项目依赖：

```powershell
.\avg_control\Scripts\python.exe -m pip install -r requirements.txt
```

默认连接参数如下，均可在运行时修改：

| 参数 | 默认值 |
| --- | --- |
| PLC 地址 | `192.168.1.11` |
| TCP 端口 | `502` |
| Slave ID | `1` |

## 运行前准备

PLC 必须已回零、处于自动模式和自动运行状态。下发每个动作前，脚本同时要求设备空闲和仓库已就绪。

物料准备取决于所选模式：

- 仓库入库：物料已在皮带机取料位，目标库位为空。
- 仓库出库：源库位有料，皮带机放料位为空。
- 线体入货：准备向皮带机送入物料，取料位可接收物料。
- 线体出货：皮带机有待输出物料，接料区域已准备好。
- 联动入库：目标库位为空，线体和取料位准备接收物料；先经线体送入，再由货叉入库。
- 联动出库：源库位有料，皮带机放料位为空，接料区域已准备好。

当前协议没有提供库位占用及物料完全输出的独立反馈，相关物料条件由现场人员确认。

## 交互运行

```powershell
.\avg_control\Scripts\python.exe warehouse_modbus.py
```

按提示输入 PLC 地址、TCP 端口、Slave ID，再选择操作。连接参数直接回车使用默认值；操作、列号、层号和运行秒数需要明确输入。无效输入会提示重新输入。

| 菜单编号 | 模式 | `--operation` 值 | 需要输入的操作参数 |
| --- | --- | --- | --- |
| 1 | 仓库出库 | `outbound` | 列号、层号 |
| 2 | 线体出货 | `line-outbound` | 运行秒数 |
| 3 | 线体入货 | `line-inbound` | 无 |
| 4 | 仓库入库 | `inbound` | 列号、层号 |
| 5 | 联动出库：仓库出库 → 线体出货 | `linked-outbound` | 列号、层号、线体运行秒数 |
| 6 | 联动入库：线体入货 → 仓库入库 | `linked-inbound` | 列号、层号 |

菜单编号 **5、6 是脚本的组合模式**，不会作为命令编号写入 PLC。参数全部收集并校验后才建立连接；输入期间按 `Ctrl+C` 或结束输入会取消运行。

## 命令行运行

### 仓库入库

将皮带机取料位的物料存入 A 库第 3 层第 4 列：

```powershell
.\avg_control\Scripts\python.exe warehouse_modbus.py --operation inbound --column 4 --level 3
```

### 仓库出库

从 A 库第 1 层第 6 列取料并放到皮带机：

```powershell
.\avg_control\Scripts\python.exe warehouse_modbus.py --operation outbound --column 6 --level 1
```

### 线体入货

开启皮带机向内输送物料，等待 PLC 检测到物料并恢复空闲：

```powershell
.\avg_control\Scripts\python.exe warehouse_modbus.py --operation line-inbound
```

### 线体出货

开启皮带机向外输送，观察到 PLC 忙碌后运行 5 秒，再清零停止：

```powershell
.\avg_control\Scripts\python.exe warehouse_modbus.py --operation line-outbound --duration 5
```

如果 PLC 提前恢复空闲，脚本会提前清零并结束运行。`--duration` 控制线体运行时长，**不代表物料已经完全输出**，实际出货情况需现场确认。

### 联动入库

线体先入货到位，然后自动将物料存入 A 库第 3 层第 4 列：

```powershell
.\avg_control\Scripts\python.exe warehouse_modbus.py --operation linked-inbound --column 4 --level 3
```

顺序：线体入货（命令 3）→ 忙碌后恢复空闲 → 清 0 → 重新等待空闲且就绪 → 写库位参数 → 仓库入库（命令 4）→ 忙碌后恢复空闲 → 清 0。

### 联动出库

先从 A 库第 1 层第 6 列出库，再自动运行线体出货 5 秒：

```powershell
.\avg_control\Scripts\python.exe warehouse_modbus.py --operation linked-outbound --column 6 --level 1 --duration 5
```

顺序：写库位参数 → 仓库出库（命令 1）→ 忙碌后恢复空闲 → 清 0 → 重新等待空闲且就绪 → 线体出货（命令 2）→ 运行指定时长或提前空闲 → 清 0 → 等待空闲。

两个步骤复用同一条 TCP 连接。第一步完成并成功清零后，才执行第二步；第一步失败或第二步无法就绪时终止后续动作。第二步失败时第一步可能已经改变物料位置，脚本不会自动回滚或重发。

### 自定义连接参数

例如连接 `192.168.1.20:1502`、Slave ID `2`，向 A 库第 2 层第 5 列入库：

```powershell
.\avg_control\Scripts\python.exe warehouse_modbus.py --plc-host 192.168.1.20 --plc-port 1502 --device-id 2 --operation inbound --column 5 --level 2
```

`--host`、`--port` 分别是 `--plc-host`、`--plc-port` 的别名。指定 `--operation` 后，未提供的连接参数使用默认值；未提供的必要库位或线体运行秒数仍会提示输入。

## 参数说明

| 参数 | 含义与限制 | 默认或缺省行为 |
| --- | --- | --- |
| `--plc-host` / `--host` | PLC IP 地址或主机名，不能为空 | `192.168.1.11`；交互模式可输入 |
| `--plc-port` / `--port` | TCP 端口，整数 1～65535 | `502`；交互模式可输入 |
| `--device-id` | Slave ID，整数 1～247 | `1`；交互模式可输入 |
| `--operation` | 上表中的六种模式之一 | 未指定且非预览时进入菜单 |
| `--column` | 列号，整数 1～6 | 仓库与联动模式需要；缺省时提示输入 |
| `--level` | 层号，整数 1～3 | 仓库与联动模式需要；缺省时提示输入 |
| `--duration` | 线体出货运行秒数，有限正数，不能大于 `--action-timeout` | 线体出货与联动出库需要；缺省时提示输入 |
| `--ready-timeout` | 每次等待空闲且就绪的超时秒数 | `30` |
| `--start-timeout` | 每次下发后等待进入忙碌的超时秒数 | `15` |
| `--action-timeout` | 每个动作完成或线体停止的等待超时秒数 | `180` |
| `--dry-run` | 预览指定流程，不连接 PLC | 关闭；使用时必须提供完整操作参数 |
| `-h` / `--help` | 显示帮助并退出 | 无 |

所有超时参数必须是有限的正数。独立线体模式不接受 `--column`、`--level`；只有 `line-outbound`、`linked-outbound` 接受 `--duration`。

超时按阶段分别计算，不是整次运行的总时限。线体出货计时从观察到忙碌开始，清零后还会单独等待停止。

## 流程预览与离线测试

建议先用 `--dry-run` 检查库位、连接配置和动作顺序。预览不会连接 PLC，也不会写寄存器：

```powershell
.\avg_control\Scripts\python.exe warehouse_modbus.py --operation linked-inbound --column 4 --level 3 --dry-run
.\avg_control\Scripts\python.exe warehouse_modbus.py --operation linked-outbound --column 6 --level 1 --duration 5 --dry-run
```

预览模式不弹出输入提示，缺少模式、库位或必要运行秒数会报参数错误。

查看完整帮助：

```powershell
.\avg_control\Scripts\python.exe warehouse_modbus.py --help
```

运行离线测试，使用模拟 PLC，不连接实机：

```powershell
.\avg_control\Scripts\python.exe -m unittest -v test_warehouse_task.py
```

测试覆盖库位范围、参数输入、单步和联动命令顺序、线体定时停止、超时、写响应丢失及连接清理。离线测试不能替代现场设备验证。

## Modbus 寄存器映射

PLC 作为 Modbus TCP 服务器，脚本使用功能码 **03** 读取保持寄存器、功能码 **06** 写单个保持寄存器。Python 使用从 0 开始的协议地址。

| 功能 | 文档地址 | Python 地址 | 权限及含义 |
| --- | --- | --- | --- |
| 设备状态 | 40001 | 0 | 只读：0 空闲、1 忙碌、2 未回零 |
| 列号 | 40002 | 1 | 只写：1～6 |
| 层号 | 40003 | 2 | 只写：1～3 |
| 目标库 | 40004 | 3 | 只写：本设备使用 1，A 库 |
| 命令 | 40005 | 4 | 只写：0 清除、1 仓库出库、2 线体出货、3 线体入货、4 仓库入库 |
| 仓库就绪 | 40006 | 5 | 只读：0 未就绪、1 已就绪 |

下发仓库动作前，先清除命令，再分别写入列号、层号、A 库；参数全部写入成功后，最后写动作命令。独立线体动作只操作命令寄存器，不写库位参数。

命令地址采用调度说明寄存器总表及截图中的 **40005**。原文部分操作步骤把命令地址写成 40004，与总表不一致；40004 在本脚本中仅用于选择 A 库。

## 状态等待、停止与 TCP 断开

每个动作开始前都检查 `设备状态 == 0` 且 `仓库就绪 == 1`。下发后必须观察到设备进入忙碌，再等待恢复空闲，避免把尚未开始的动作误判为已完成。

线体入货由 PLC 的物料检测结束；未检测到物料导致动作超时时，脚本会尝试清零。线体出货按指定运行秒数停止，运行中会轮询设备状态；正常通信下可用 `Ctrl+C` 中断当前操作。

正常结束、通信异常、动作超时或 `Ctrl+C` 时，主程序的 `finally` 调用 `plc.close()`。如果本次可能仍有活动命令，先尝试清 0；即使清零失败，也会执行 `client.close()` 关闭 TCP 连接。

清 0 是撤销命令或停止皮带机的处理，不等同于机械急停；关闭 TCP 连接也不代表设备一定停止。通信中断可能导致清零失败，程序会报告设备状态未确认，此时需现场处理。强制结束进程无法保证清理代码执行。

操作正常结束返回退出码 `0`，输入取消、连接或动作失败、中断及清零失败返回 `1`；命令行参数错误由参数解析器返回 `2`。

## 常见问题

| 提示或现象 | 原因及检查方向 |
| --- | --- |
| `PLC 连接失败` | 检查 PLC 地址、端口、网络和 Modbus TCP 服务是否与配置一致 |
| `等待空闲/就绪超时，设备状态=0，就绪=0` | 设备空闲但未就绪；需检查自动模式、自动运行状态及 PLC 就绪条件。只延长超时不能使设备就绪 |
| `设备未回零` | 状态为 2；先在设备端完成回零，再重新运行 |
| `等待动作进入忙碌状态超时` | 下发后未观察到状态 1；检查动作是否被接受、寄存器映射及 PLC 状态反馈，不要直接跳过握手等待 |
| `等待动作完成超时` | 动作没有在时限内恢复空闲；检查设备和物料。线体入货时需检查物料是否到位 |
| `命令清 0 失败` | 命令撤销未确认；需现场检查和停止设备，再决定后续操作 |
| 联动第一步完成，第二步失败 | 物料可能已移动；按日志核对现场位置，处理故障后再选择合适的单步或联动操作 |

如使用 Modbus Slave 模拟器，就绪初始值可在模拟器端设置为地址 0 = 0、地址 5 = 1；验证完整动作还需要模拟忙碌和空闲的变化。真实 PLC 的设备状态和就绪寄存器是只读的，由 PLC 程序更新，脚本不会写入。
