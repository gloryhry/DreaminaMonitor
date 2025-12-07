# Dreamina Manager

Dreamina Manager 是一个基于 Python 和 Vue.js 的多账户管理与 API 代理转发系统。它旨在帮助用户管理多个 Dreamina 账户，并通过统一的 API 接口实现负载均衡和自动故障转移。

## ✨ 功能特性

### 核心功能

*   **多账户管理**：集中管理多个 Dreamina 账户，支持添加、编辑、删除和禁用账户。
*   **批量导入账户**：支持一次性批量导入多个账户 (`email:password` 格式)，自动去重。
*   **智能代理转发**：
    *   拦截 `/v1/*` 路径的请求（如 `/v1/images/generations`）。
    *   自动轮询活跃账户池，实现请求负载均衡。
    *   自动替换 `Authorization` 头为对应的 `region-session_id`。
    *   支持上游错误检测（429, 500, 524），自动临时禁用故障账户。

### 自动化任务

*   **自动注册账户**：支持配置自动注册间隔，由系统自动调用 Dreamina-register API 创建新账户。
*   **Session 自动更新**：定期检查并更新超过指定天数的 `session_id`，保持账户活跃。
*   **自动解禁**：后台任务每分钟检查禁用账户，到期自动解禁。
*   **使用次数重置**：在设定时间自动重置所有账户的使用次数。
*   **积分自动查询**：
    *   每次账户被使用后自动查询并更新积分。
    *   每日定时批量更新所有账户积分（CN 区域除外）。

### 详细使用统计

*   按模型分别统计调用次数：
    *   Jimeng 4.0 / 4.1
    *   Nanobanana / NanobananaPro
    *   Video 3.0
*   记录账户错误次数和最后活跃时间。
*   非 `nanobanana`/`nanobananapro` 模型请求时，积分为 0 的账户自动排除。

### 账户区域支持

*   支持多区域账户管理：`US`、`HK`、`JP`、`SG`、`CN`
*   自动根据 `session_id` 前缀识别区域
*   CN 区域账户不支持积分查询（跳过）

### 现代化仪表盘

*   基于 Vue.js 3 和 TailwindCSS 构建的单页应用 (SPA)。
*   **实时监控**：表格数据每 5 秒自动刷新。
*   **响应式布局**：表格高度自适应，支持固定列（邮箱、状态、操作）和横向滚动。
*   支持按邮箱和区域筛选账户。
*   一键注册新账户 / 更新 Session ID。

## 🛠️ 技术栈

*   **后端**：Python 3.12+, FastAPI, Uvicorn, aiosqlite (SQLite)
*   **前端**：Vue.js 3 (CDN), TailwindCSS (CDN), Axios
*   **包管理**：uv
*   **配置管理**：Pydantic Settings + config.json

## 📁 项目结构

```
DreaminaMonitor/
├── main.py         # 主入口，后台任务调度
├── api.py          # API 路由（账户管理、设置）
├── proxy.py        # 代理转发逻辑、积分查询
├── database.py     # 数据库模型与连接
├── config.py       # 配置管理
├── config.json     # 配置文件（自动生成）
├── static/
│   └── index.html  # 前端 SPA
└── dreamina.db     # SQLite 数据库
```

## 🚀 快速开始

### 前置要求

*   Python 3.12 或更高版本
*   [uv](https://github.com/astral-sh/uv) (推荐) 或 pip

### 安装与运行

1.  **克隆项目**
    ```bash
    git clone <repository_url>
    cd DreaminaMonitor
    ```

2.  **安装依赖**
    使用 `uv` 初始化并安装依赖：
    ```bash
    uv sync
    ```
    或者使用 pip：
    ```bash
    pip install -r requirements.txt
    ```

3.  **配置文件**
    项目启动时会自动生成 `config.json`。你可以修改此文件或设置环境变量。
    
    默认管理员密码为：`admin123` (请务必修改！)

4.  **启动服务**
    ```bash
    uv run python main.py
    ```
    或
    ```bash
    uv run uvicorn main:app --host 0.0.0.0 --port 5100 --reload
    ```
    服务将在 `http://localhost:5100` 启动。

### Docker 部署

**方式一：使用预构建镜像（推荐）**

```bash
# 创建 docker-compose.yml
cat > docker-compose.yml << 'EOF'
services:
  dreamina-monitor:
    image: ghcr.io/gloryhry/dreaminamonitor:latest
    container_name: dreamina-monitor
    ports:
      - "5100:5100"
    volumes:
      - ./dreamina.db:/app/dreamina.db
      - ./config.json:/app/config.json
    environment:
      - TZ=Asia/Shanghai
    restart: unless-stopped
EOF

# 启动服务
docker-compose up -d
```

**方式二：本地构建**

```bash
git clone https://github.com/gloryhry/DreaminaMonitor.git
cd DreaminaMonitor
docker-compose up -d --build
```

**常用命令**

```bash
docker-compose logs -f    # 查看日志
docker-compose down       # 停止服务
docker-compose pull       # 更新镜像
```

> **注意**：Docker 部署会自动挂载 `dreamina.db` 和 `config.json`，数据持久化存储在宿主机。

## ⚙️ 配置说明

配置优先级：`config.json` > 环境变量 > 默认值。

### 基础设置

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `ADMIN_PASSWORD` | `admin123` | 管理员密码，用于前端登录和 API 认证 |
| `UPSTREAM_BASE_URL` | `http://localhost:8080` | 上游 API 地址 |
| `HOST` | `0.0.0.0` | 服务监听地址 |
| `PORT` | `5100` | 服务监听端口 |
| `PROXY_TIMEOUT` | `300` | 代理请求超时时间（秒） |
| `LOG_LEVEL` | `info` | 日志等级 (debug/info/warning/error/critical) |

### 模型使用限制

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `LIMIT_JIMENG_4_0` | `60` | Jimeng 4.0 每日调用限制 |
| `LIMIT_JIMENG_4_1` | `60` | Jimeng 4.1 每日调用限制 |
| `LIMIT_NANOBANANA` | `60` | Nanobanana 每日调用限制 |
| `LIMIT_NANOBANANAPRO` | `60` | NanobananaPro 每日调用限制 |
| `LIMIT_VIDEO_3_0` | `60` | Video 3.0 每日调用限制 |

### 自动化任务设置

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `RESET_COUNTS_TIME` | `00:00` | 每日重置使用次数的时间 |
| `SESSION_UPDATE_DAYS` | `7` | Session 超过多少天后自动更新 |
| `SESSION_UPDATE_BATCH_SIZE` | `5` | Session 批量更新时的批次大小 |
| `AUTO_REGISTER_ENABLED` | `false` | 是否启用自动注册 |
| `AUTO_REGISTER_INTERVAL` | `3600` | 自动注册间隔（秒） |

### Dreamina-register API 设置

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `REGISTER_API_URL` | `null` | Dreamina-register API 地址 |
| `REGISTER_API_KEY` | `null` | Dreamina-register API 密钥 |
| `REGISTER_MAIL_TYPE` | `moemail` | 注册使用的邮箱类型 |
| `DEFAULT_POINTS` | `120.0` | 新注册账户的默认积分 |

## 🖥️ 前端使用

访问 `http://localhost:5100` 进入管理后台。

1.  **登录**：输入配置的管理员密码。
2.  **Dashboard**：
    *   查看所有账户状态、积分和各模型使用情况。
    *   点击 **Add Account** 添加新账户。
    *   点击 **Bulk Add** 批量导入账户。
    *   点击 **Register** 自动注册新账户（需配置 Dreamina-register API）。
    *   点击 **Edit** 修改账户信息。
    *   点击 **Update Session** 更新账户的 Session ID。
    *   点击 **Ban/Unban** 手动禁用或解禁账户。
3.  **Settings**：在线修改系统配置并保存（无需重启）。

## 🔌 API 接口

### 代理接口

*   `POST /v1/*`：代理转发接口。
    *   Header: `Authorization: Bearer <ADMIN_PASSWORD>`
    *   功能：自动选择账户并转发请求到上游。

### 账户管理接口

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| `GET` | `/api/accounts` | 获取账户列表（分页、筛选） |
| `POST` | `/api/accounts` | 添加账户 |
| `POST` | `/api/accounts/bulk` | 批量添加账户 |
| `PUT` | `/api/accounts/{id}` | 更新账户 |
| `DELETE` | `/api/accounts/{id}` | 删除账户 |
| `POST` | `/api/accounts/{id}/ban` | 禁用账户 |
| `POST` | `/api/accounts/{id}/unban` | 解禁账户 |
| `POST` | `/api/accounts/register` | 注册新账户 |
| `POST` | `/api/accounts/{id}/session/update` | 更新 Session ID |

### 设置接口

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| `GET` | `/api/settings` | 获取设置 |
| `POST` | `/api/settings` | 更新设置 |

## 📝 注意事项

*   **安全性**：请务必在部署后立即修改默认的 `ADMIN_PASSWORD`。
*   **数据库**：数据存储在本地 `dreamina.db` SQLite 数据库中，请定期备份。
*   **Session ID**：请确保添加的 Session ID 有效且格式正确。
*   **CN 区域**：中国区域账户不支持积分查询，积分相关功能会自动跳过。

## 📄 License

MIT License
