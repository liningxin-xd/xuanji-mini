# 下载与安装错误码字典

## 来源与适用边界

- 源码路径：`infra/tapfiledownload/tap-filedownload/src/main/java/com/taptap/tapfiledownload/exceptions/`
- 源码快照最后更新：`2026-04-24`
- 下载命名空间：TapFileDownload `TapDownException`
- 安装命名空间：Android `PackageInstaller.STATUS_*`
- 客户端版本生效区间：未登记

本文件只在 Playbook 的错误码模块已经触发、错误码来源已经确认后读取。优先使用事件来源、链路阶段和原始类型区分命名空间；“四位数字属于下载、负数属于安装”只作为格式校验，不能覆盖来源语义。下载错误码必须保留前导零；若存储层把 `02xx` 变成三位数字，且没有已登记的还原规则，则含义保持未确认。

该字典是上述日期的源码快照，不代表所有历史和未来客户端版本都使用同一映射。事件版本无法与该快照建立适用关系、编码来源不明或一码多义时，只保留原编码并在 `evidence_limits` 标记含义未确认。

## 下载错误码

### 下载失败事件来源

以下登记来自复杂版分析文档，其中指标设计、固定阈值和 SQL 尚未通过数仓或 BI 复核，因此不进入本字典。这里只登记错误码模块执行所需的数据源和字段语义：

| 项目 | 登记值 |
|---|---|
| 适用范围 | TapTap 国内、Android、APK 游戏下载链路 |
| 明细表 | `tap_dw.dwd_str_game_core_behavior_di` |
| 引擎与时效 | MaxCompute DWD 明细，T+1 |
| 分区 | `dt`；查询必须裁剪目标与基线日期 |
| 失败行为 | `behavior_type='game_download_failed'`，对应 action `appDownloadNewFailed` |
| 错误码 | `GET_JSON_OBJECT(action_args, '$.code')` |
| 错误描述 | `GET_JSON_OBJECT(action_args, '$.info')` |
| 下载链路键 | `chain_id`，使用前仍须通过 Playbook 的覆盖与冲突门禁 |
| 公共过滤 | `platform='ANDROID' AND game_type='app' AND is_risk_device=0` |
| 受影响实体 | `dt + device_id + game_id`；不得用字符串直接拼接代替结构化复合键 |

计算错误实体率或末态分类时，先按受影响实体收敛；计算失败 PV 和每实体 Retry 次数时保留事件计数。`action_args.code` 为 NULL、空串、格式异常或来源不明时进入 `unmatched_code` 质量桶，不得并入网络或任何业务类别。T+1 分区未成熟、失败 action 覆盖与 `behavior_type` 不一致或关联后行数放大时停止该模块。

下载错误码由四位十进制文本组成：

```text
error_code = errorPrefix(2 位) + pos(2 位)
```

前两位表示异常类型，后两位表示抛出位置。例如 `1121` 表示 `11`（`TapDownOtherException`）和 `21`（dispatch enqueue error）。`pos` 只定位代码位置，不单独证明技术根因。

### 异常前缀

| 前缀 | 异常类 | 代码含义 | 用户提示 |
|---|---|---|---|
| `02xx` | `TapDownConnectionTimeOutException` | 连接超时 | 连接服务超时，请检查网络后重试 |
| `03xx` | `TapDownFileException` | 文件操作失败，例如创建失败 | 创建文件失败，请重试 |
| `04xx` | `TapDownFileSizeException` | 下载后文件大小不匹配 | MD5 校验失败，与 `06xx` 使用同类提示 |
| `05xx` | `TapDownMakeConnectionException` | 服务器无响应 | 服务器无响应，请稍后重试 |
| `06xx` | `TapDownMd5Exception` | MD5 校验失败 | 文件校验失败，请重新下载 |
| `07xx` | `TapDownMkDirException` | 创建目录失败 | 创建 APK/OBB 目录失败 |
| `08xx` | `TapDownNotEnoughSpaceException` | 磁盘空间不足 | 手机存储空间不足 |
| `09xx` | `TapDownOpenConnectionException` | 打开连接失败 | 打开网络连接失败 |
| `10xx` | `TapDownOpenInputException` | 打开输入流失败 | 打开输入流失败 |
| `11xx` | `TapDownOtherException` | 其他错误兜底 | 下载失败，请重试 |
| `12xx` | `TapDownReadInputException` | 读取数据失败 | 读取下载数据失败 |
| `13xx` | `TapDownReadTimeOutException` | 读取超时 | 读取数据超时，请检查网络 |
| `14xx` | `TapDownRetryException` | 重试耗尽或续传失败 | 下载失败，请重试 |
| `15xx` | `TapDownServerException` | 服务器错误，后两位关联 HTTP 状态码处理位置 | HTTP 错误，显示具体状态码 |
| `16xx` | `TapDownURLErrorException` | URL 格式错误 | 下载地址无效 |
| `17xx` | `TapDownURLFetchException` | 下载 URL 获取失败 | 无法获取下载地址 |
| `18xx` | `TapDownWriteOutputException` | 写入数据失败 | 写入文件失败 |
| `19xx` | `TapDownWritePermissionException` | 无写入权限 | 无存储写入权限 |
| `20xx` | `TapDownFileNotExistException` | 本地校验发现文件不存在 | 文件丢失，需重新下载 |
| `21xx` | `TapDownMergeException` | 省流量更新或 Patch 合并失败 | 省流量更新失败，可在设置中关闭后重新下载 |
| `22xx` | `TapDownReNameException` | 文件重命名失败 | 文件重命名失败 |
| `23xx` | `TapDownPathConflictException` | 多任务下载路径冲突 | 文件路径冲突，多任务并发 |

### `11xx` 常见位置

| 错误码 | 触发位置 | 代码说明 |
|---|---|---|
| `1121` | `FileDownloadV3.kt` | `task.start()` 返回 `0`，任务加入队列失败 |
| `1104` | `DownloadDispatcher` | 分发入队异常 |
| `1105` | `DownloadCall` | URL 预处理失败 |
| `1106` | `DownloadCall` | 网络连接其他异常 |
| `1109` | `DownloadChain` | 链式调用中断 |
| `1110` | `RetryInterceptor` | 重试拦截器异常 |
| `1116` | `DownloadChain` | 链式异常兜底 |

### 待复核观测分类

下表来自 AI 生成的复杂版分析文档，只能作为查询后的排查方向，不能覆盖前述源码前缀和精确位置定义。只有错误码、脱敏 `info` 类别、事件日期和来源相互一致时，才可用它校准 `summary` 或 `recommended_action`。

源码定义与观测分类回答的问题不同：源码定义说明编码命名空间、异常类型和抛出位置；观测分类说明特定历史样本中该码主要伴随的故障症状。两者可以同时成立。观测分类未被源码快照覆盖时，应标记覆盖缺口和证据层级，不因此直接判为语义矛盾。

| 观测类别 | 复杂版列出的错误码 | 典型脱敏 `info` | 排查方向 |
|---|---|---|---|
| 网络 | `0500`, `1116`, `1300`, `1200`, `0200` | unable to resolve host、connection abort、read timeout | 网络、CDN、连接链路 |
| 客户端异常或调度 | `1121`, `2200`, `1502`, `2203`, `1700`, `1900`, `2400`, `2101`, `2102`, `1118`, `1106`, `2000` | dispatch enqueue error、downloader exception | 客户端下载器、任务调度 |
| APK 包或校验 | `0801`, `0601`, `0400`, `1500`, `0401`, `0701` | APK、MD5、header mismatch、HTTP 416 | 包体、校验、分发 |
| 存储或路径 | `0802`, `1800`, `0703`, `0702`, `0700`, `1108` | free space insufficient、ENOSPC、mkdir failed | 存储、目录、路径处理 |

使用该分类时必须遵守：

- `NULL` 或缺失 code 保留为 `unmatched_code`，不得按复杂版原规则直接归入网络。
- `0801` 正在确认。当前观测分类为 APK 解析，而已提供源码快照中 `08xx` 表示空间不足；在命名空间、客户端版本和对应 `info` 复核完成前保持含义未确认，不进入 APK 或存储类别。
- `2400` 在观测数据中暂归客户端异常或调度，但当前源码快照只登记到 `23xx`。这是源码快照覆盖缺口，不是已确认的语义矛盾；可保留为观测分类线索，同时标记其源码异常类型尚未登记。
- `1116` 的源码语义是 `DownloadChain` 链式异常兜底，观测语义是历史样本中网络症状占主。前者说明抛出位置，后者说明样本分布，两者不矛盾：有已校验的网络类 `info` 时可进入网络排查；缺少 `info` 或出现混合症状时，只能归为链式兜底，不能仅凭该码确认网络问题。
- 分类中的排查方向不是责任归属。任何类别都必须报告实体影响面和 Retry，不得按码数或 PV 相加为原因贡献。最终失败和恢复只有在独立事件语义、链路键、时序和状态机 Contract 启用后才能报告；当前 1.8-B 不提供这两个字段。

## 安装错误码

安装错误码直接使用 Android `PackageInstaller` 的 `STATUS_*` 常量，不使用 TapDownException 的四位编码。`STATUS_SUCCESS = 0` 是成功状态，不得计为错误；负数状态必须结合安装事件来源解释。

| PackageInstaller 状态 | 数值 | `InstallFailNotifyType` | 代码含义 |
|---|---:|---|---|
| `STATUS_SUCCESS` | `0` | - | 安装成功 |
| `STATUS_FAILURE` | `-1` | `OTHER` | 通用失败 |
| `STATUS_FAILURE_ABORTED` | `-2` | `USER_CANCEL` | 用户取消或超时取消；仅凭状态码不能区分二者 |
| `STATUS_FAILURE_INCOMPATIBLE` | `-3` | `OLDER_SDK` / `OTHER` | 设备不兼容，包括 `minSdk` 不满足 |
| `STATUS_FAILURE_INVALID` | `-4` | `PARSE_APK_FAILED` / `VERSION_DOWNGRADE` / `OLDER_SDK` / `BAD_SIGNATURE` | APK 无效；子类型按 message 映射 |
| `STATUS_FAILURE_CONFLICT` | `-5` | `OTHER` | 与已安装包冲突 |
| `STATUS_FAILURE_STORAGE` | `-6` | `STORAGE_NOT_ENOUGH` | 存储空间不足 |
| `STATUS_FAILURE_TIMEOUT` | `-7` | `TIMEOUT` | 安装回调超时 |

### `STATUS_FAILURE_INVALID` 子类型

只在原始状态为 `STATUS_FAILURE_INVALID` 时使用 message 分类。message 必须来自已登记字段，最终输出只保留脱敏类别，不输出原文。

| message 包含 | `InstallFailNotifyType` | 含义 |
|---|---|---|
| `INSTALL_FAILED_VERSION_DOWNGRADE` | `VERSION_DOWNGRADE` | 降级安装 |
| `INSTALL_FAILED_OLDER_SDK` | `OLDER_SDK` | 系统版本过低 |
| `INSTALL_FAILED_BAD_SIGNATURE` | `BAD_SIGNATURE` | 签名校验失败 |
| 其他 | `PARSE_APK_FAILED` | APK 解析失败兜底 |

## 排查线索与结论边界

- `1121` 的确定含义是任务入队失败。队列容量或内部调度异常只是后续排查方向；是否可由重试恢复必须用实体重试数和最终完成数据验证。
- `04xx` 和 `06xx` 指向文件大小或 MD5 校验失败。下载中文件损坏及重新下载恢复属于待验证方向，不能仅凭错误码确认。
- `08xx` 和 `STATUS_FAILURE_STORAGE` 指向存储空间不足；仍需结合存储字段覆盖、实体影响面和最终恢复判断业务影响。
- 错误码只校准现有候选的机制方向，不创建贡献候选，不得把多个错误码的 PV 或实体数相加为原因贡献。

## 相关代码位置

| 内容 | 路径 |
|---|---|
| 异常类定义 | `infra/tapfiledownload/tap-filedownload/.../exceptions/TapDown*.kt` |
| 错误码计算 | `TapDownException.kt -> errorNo` |
| 下载错误用户提示 | `feat/game/game-downloader/impl/.../exception/AppDownloadException.kt` |
| 安装错误处理 | `feat/game/game-installer/impl/.../v2/GameInstallManager.kt -> handleTapInstallError()` |
| 安装失败类型枚举 | `feat/biz/game/api/.../data/InstallFailNotifyType.kt` |
