# Docker 模式支持 Codex CLI 设计

## 背景

TickFlow 的 Docker 服务运行在独立容器中。即使 macOS 主机已经安装并登录 Codex CLI，容器内的 `shutil.which("codex")` 仍无法找到主机命令，因此设置页显示“未找到 Codex CLI 命令: codex”。

## 目标

- Docker 镜像内提供可直接执行的 `codex` 命令。
- 复用主机已有的 Codex 登录凭据，无需在容器内重复交互式登录。
- 保持现有 `codex exec`、临时工作目录、只读沙箱和隔离 `CODEX_HOME` 逻辑不变。
- 保持镜像构建可复现，并允许维护者显式升级 Codex CLI 版本。

## 非目标

- 不允许用户配置任意可执行文件路径。
- 不改变本机开发模式和桌面客户端的 Codex 命令解析逻辑。
- 不把 Codex 凭据复制进镜像或提交到仓库。
- 不为 Dockerfile 文本结构增加脆弱的单元测试。

## 方案

### 镜像构建

Dockerfile 新增独立的 `codex-builder` 阶段，使用 Node bookworm 镜像安装固定版本的 `@openai/codex`，再从官方包中提取当前构建平台的 Linux 原生二进制。版本由 `CODEX_CLI_VERSION` 构建参数控制，并提供项目验证过的默认值。

运行阶段只把提取后的原生二进制复制到 `/usr/local/bin/codex`，不复制 npm 包，也不要求为了 Codex 常驻安装 Node.js。这样保留多架构构建能力，同时减少运行层体积和可变依赖。

### 凭据挂载

`docker-compose.yml` 将主机 `${HOME}/.codex` 挂载到容器 `/root/.codex`，模式为只读。挂载目录不会写入镜像或仓库。

后端现有 `_prepare_codex_home` 会从只读挂载中读取 `auth.json` 和兼容配置，再复制或生成到单次请求的临时 `CODEX_HOME`。Codex 子进程只使用临时目录，因此不会修改主机凭据目录。

### 数据流

1. Compose 启动容器并只读挂载主机 Codex home。
2. 设置接口调用 `codex_cli_available()`。
3. `_resolve_command("codex")` 在容器 PATH 中找到镜像内入口。
4. 发起 AI 请求时，后端建立临时工作区和临时 `CODEX_HOME`。
5. 后端复用挂载目录中的认证信息并执行 `codex exec`。
6. 请求结束后临时目录自动删除。

## 错误处理

- 主机未安装 Codex 不影响容器命令，因为 CLI 已包含在镜像中。
- 主机未登录或 `${HOME}/.codex/auth.json` 不存在时，设置页仍可识别 CLI；实际调用会返回现有的 Codex 登录错误。
- 构建时无法下载指定 npm 包时，镜像构建应失败，不静默切换到不固定版本。
- 挂载目录保持只读；任何意外写入都会由容器文件系统拒绝。

## 安全性

- Codex home 仅以只读方式挂载。
- 凭据不进入 Docker build context 的产物层。
- 现有 `--ephemeral`、`--sandbox read-only`、`approval_policy = "never"` 和空白临时工作区保持不变。
- 文档明确说明：启用 Docker Codex 模式意味着 TickFlow 容器能够读取 Codex 登录凭据，应仅在受信任的本机环境使用。

## 验证

1. 构建镜像成功。
2. 容器内 `command -v codex` 返回 `/usr/local/bin/codex`。
3. 容器内 `codex --version` 返回设计中固定的版本。
4. 设置接口返回 `ai_configured: true`，页面不再显示“未找到 Codex CLI 命令”。
5. 执行一次真实连接测试，确认 `codex exec` 能读取认证并返回内容。
6. 运行现有后端 AI provider 测试，确认非 Docker 路径无回归。

## 文档更新

README 的 Docker 启动说明增加 Codex CLI 凭据挂载、安全边界和版本覆盖方式，避免用户把“主机已安装”误认为“容器自动可见”。
