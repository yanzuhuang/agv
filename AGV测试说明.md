# 按 AGV 文档进行 TCP 测试

已经读取当前目录的 `AGV远程调度说明(1).docx` 和 `AGV与调度系统通信协议（通信篇）V1.0.2(1).pdf`。`agv_tcp.py` 根据 Word 的常用指令表生成报文，并按照 PDF 的帧格式和状态字段接收、校验、解析应答。本页的 TCP 测试脚本只依赖 Python 标准库，无需 pip 安装。

完整的仓库出库和 AGV 搬运流程见 [AGV搬运说明.md](AGV搬运说明.md)，所有控制代码和停止退出代码均在 `outbound_agv.py` 中，停止命令为 `python outbound_agv.py --operation stop`。仓库联动需要 `pymodbus`，当前 `agv_control` 环境内已安装。

## 两台电脑测试

电脑 A 编写和运行发送程序；电脑 B 运行接收脚本模拟 AGV。电脑 B 的静态 IP 假设为 `192.168.1.100`，请替换成实际地址。两台电脑的网络配置、防火墙和本机测试步骤见 [TCP测试说明.md](TCP测试说明.md)。

电脑 B 只需复制 `tcp_receiver.py`，在其所在目录运行：

```powershell
python tcp_receiver.py
```

电脑 A 在 `agv_tcp.py` 所在目录运行，向电脑 B 下发站点 2 报文：

```powershell
python agv_tcp.py --host 192.168.1.100 --command station --station 2 --send-only
```

电脑 B 应打印以下 16 字节，末字节 `B2` 是自动计算的校验和：

```text
HEX: AA 01 06 02 00 00 00 00 00 00 00 00 00 00 FF B2
```

继续测试后退和状态查询报文：

```powershell
python agv_tcp.py --host 192.168.1.100 --command backward --send-only
python agv_tcp.py --host 192.168.1.100 --command status --send-only
```

预期分别收到：

```text
AA 01 01 01 00 00 00 00 00 00 00 00 00 00 FF AC
AA 01 05 01 00 00 00 00 00 00 00 00 00 00 FF B0
```

`--send-only` 用于老师要求的 HEX 接收测试，因为 `tcp_receiver.py` 不回协议应答。默认目标为 `127.0.0.1`，便于同机测试；跨电脑必须通过 `--host` 指定电脑 B 的实际静态 IP。`0.0.0.0` 是接收端监听地址，不能代替发送端的目标 IP。

不连接任何设备，仅检查生成的帧：

```powershell
python agv_tcp.py --command station --station 2 --dry-run
```

## 常用指令

| --command | 功能 | 功能码 | 数据码 |
| --- | --- | --- | --- |
| station --station N | 下发站点 N（1～255；Word 列出 1～10） | 06 | N |
| clear-stations | 清除所有目标站点 | 06 | 00 |
| forward | 前进运行 | 01 | 00 |
| backward | 后退运行 | 01 | 01 |
| stop | 全部停止 | 02 | 00 |
| slow-stop | 减速停止 | 03 | 00 |
| status | 状态查询 | 05 | 01（可选 00） |
| roller-stop | 滚筒停止 | 17 | 00 |
| roller-forward-send | 滚筒正转送料 | 17 | 01 |
| roller-backward-send | 滚筒反转送料 | 17 | 02 |
| roller-forward-receive | 滚筒正转接料 | 17 | 03 |
| roller-backward-receive | 滚筒反转接料 | 17 | 04 |

表中功能码和数据码均为十六进制；命令行的站点、端口和 ID 参数填十进制。`--agv-id` 默认为 1，`--port` 默认为 1024，`--timeout` 默认为 5 秒。

## 使用真实 AGV

Word 中的接口配置为：AGV 是 TCP Server，IP 为 `192.168.1.16`、端口 1024、ID 1、子网掩码 `255.255.0.0`；发送电脑是 TCP Client。实际连接时以设备当前配置为准。

查询当前状态：

```powershell
python agv_tcp.py --host 192.168.1.16 --command status
```

下发站点和启动是两条独立指令。按 Word 的站点 2、后退示例，分别运行：

```powershell
python agv_tcp.py --host 192.168.1.16 --command station --station 2
python agv_tcp.py --host 192.168.1.16 --command backward
```

脚本每次连接、发送一条指令、等待匹配的应答并关闭连接。接收时按固定 16 字节拆包，检查 AA/FF 和校验和；忽略 ID 或功能码不匹配的帧，直至收到对应应答或超时。任何超时都不会自动重发，避免重复下发动作。收到指令应答只代表收到该应答，不能据此判定已经到站或完成装卸。

状态查询会显示地标号、当前站点、速度百分比、状态、挂钩、电池电压、报警 IO 原始位和有输入的 IO 编号。报警 IO 的 bit0 为驱动异常、bit1 为牵引棒异常、bit2 为急停按下，其余位为备用。

## 文档差异与采用的规则

- **查询数据码：** Word 为 `AA 01 05 01 ... FF B0`，PDF 第 12 页为 `AA 01 05 00 ... FF AF`。默认采用 Word。若设备使用 PDF 示例，可明确运行 `python agv_tcp.py --host 192.168.1.16 --command status --query-data 0`。程序不自动切换或补发。
- **滚筒名称：** Word 将数据 1/2 写为送料、3/4 写为接料；PDF 第 15 页将 1/2 写为上料、3/4 写为下料。代码的命令名称按 Word，字节值与两份文件的示例一致，实际动作含义需要按设备配置确认。
- **长度与校验：** PDF 2.5.2 表格明确应答为 16 字节、预留 9 字节、校验 1 字节，后文却写预留 8 字节和校验 2 字节。采用表格和 Word 抓包截图的 16 字节格式，校验为前 15 字节之和 `% 256`，不是 CRC。
- **数据字节序：** 指令表格标明数据码低位在前，但文字举例 `01 02 = 258` 与此不一致。本实现按表格；目前公开命令的数据都不超过 255，和 Word 示例一致。状态帧地标号按 PDF 2.5.1 的高位在前解析。
- **应答不是原样回显：** Word 截图下发站点 2，返回的是功能码 06、数据码 1 的应答。因此代码匹配 ID 和功能码并打印应答数据，不要求应答与发送帧完全相同；文档未完整定义其他应答数据的成功/失败含义，程序不猜测。
- **电量字段：** PDF 用 `16 20` 举例表示 `22.32 V`，所以按两个字节的十进制整数和小数显示电压，不显示为电量百分比。
- **输入 IO：** PDF 将 bit0 和 bit1 都写成“物料传感器1”，描述不明确；代码只显示输入编号 1～8，不推断物料到位。
- **版本：** PDF 文件名含 V1.0.2，内部页眉标注 V1.0.1。本实现依据实际正文、表格和示例。

仓库 PLC 的 `warehouse_modbus.py` 使用 Modbus TCP，与这里的 AGV 原始 TCP 协议分开使用。

## 已完成的本地验证

运行 `python -m unittest -v test_agv_tcp.py` 可执行 7 项测试，其中直接读取原 Word 表格，对照全部 18 条常用指令。测试还覆盖错误校验和、逐字节拆包、多帧粘包、状态字段、应答匹配以及超时不重发；网络测试只连接本地回环地址。

已实际启动 `127.0.0.1:1024` 接收端，分别发送站点 2、后退、状态查询及任意 HEX 示例，在接收端确认对应字节完整打印；同时保持一个空闲连接，其他客户端仍可正常发送。尚未使用两台实体电脑或真实 AGV 验证，跨电脑测试请按上述步骤执行。
