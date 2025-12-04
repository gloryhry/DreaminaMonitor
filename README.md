# Dreamina Manager

Dreamina Manager 是一个基于 Python 和 Vue.js 的多账户管理与 API 代理转发系统。它旨在帮助用户管理多个 Dreamina 账户，并通过统一的 API 接口实现负载均衡和自动故障转移。

## ✨ 功能特性

*   **多账户管理**：集中管理多个 Dreamina 账户，支持添加、编辑、删除和禁用账户。
*   **智能代理转发**：
    *   拦截 `/v1/*` 路径的请求（如 `/v1/images/generations`）。
    *   自动轮询活跃账户池，实现请求负载均衡。
    *   自动替换 `Authorization` 头为对应的 `region-session_id`。
    *   支持上游错误检测（429, 500, 524），自动临时禁用故障账户（默认 12 小时）。
*   **详细使用统计**：
    *   按模型分别统计调用次数（Jimeng 4.0, Jimeng 4.1, Nanobanana, NanobananaPro, Video 3.0）。
    *   记录账户错误次数和最后活跃时间。
*   **现代化仪表盘**：
    *   基于 Vue.js 3 和 TailwindCSS 构建的单页应用 (SPA)。
    *   **实时监控**：表格数据每 5 秒自动刷新。
    *   **响应式布局**：表格高度自适应，支持固定列（邮箱、状态、操作）和横向滚动。
    *   支持按邮箱和区域筛选账户。
*   **灵活配置**：支持 `config.json` 配置文件和环境变量双重配置。

## 🛠️ 技术栈

*   **后端**：Python 3.12+, FastAPI, Uvicorn, aiosqlite (SQLite)
*   **前端**：Vue.js 3 (CDN), TailwindCSS (CDN), Axios
*   **包管理**：uv

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
    uv run uvicorn main:app --host 0.0.0.0 --port 5100 --reload
    ```
    服务将在 `http://localhost:5100` 启动。

## ⚙️ 配置说明

配置优先级：`config.json` > 环境变量 > 默认值。

| 配置项 | 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `ADMIN_PASSWORD` | `ADMIN_PASSWORD` | `admin123` | 管理员密码，用于前端登录和 API 认证 |
| `UPSTREAM_BASE_URL` | `UPSTREAM_BASE_URL` | `https://jimeng.985100.xyz` | 上游 API 地址 |
| `BAN_DURATION_HOURS` | `BAN_DURATION_HOURS` | `12` | 触发错误后账户禁用的时长（小时） |
| `LIMIT_JIMENG_4_0` | `LIMIT_JIMENG_4_0` | `60` | Jimeng 4.0 每日调用限制 |
| `LIMIT_JIMENG_4_1` | `LIMIT_JIMENG_4_1` | `60` | Jimeng 4.1 每日调用限制 |
| `LIMIT_NANOBANANA` | `LIMIT_NANOBANANA` | `60` | Nanobanana 每日调用限制 |
| `LIMIT_VIDEO_3_0` | `LIMIT_VIDEO_3_0` | `60` | Video 3.0 每日调用限制 |

## 🖥️ 前端使用

访问 `http://localhost:5100` 进入管理后台。

1.  **登录**：输入配置的管理员密码。
2.  **Dashboard**：
    *   查看所有账户状态、积分和各模型使用情况。
    *   点击 **Add Account** 添加新账户。
    *   点击 **Edit** 修改账户信息（如更新 Session ID）。
    *   点击 **Ban/Unban** 手动禁用或解禁账户。
3.  **Settings**：在线修改系统配置并保存（无需重启）。

## 🔌 API 接口

### 代理接口
*   `POST /v1/*`：代理转发接口。
    *   Header: `Authorization: Bearer <ADMIN_PASSWORD>`
    *   功能：自动选择账户并转发请求到上游。

### 管理接口
*   `GET /api/accounts`：获取账户列表
*   `POST /api/accounts`：添加账户
*   `PUT /api/accounts/{id}`：更新账户
*   `DELETE /api/accounts/{id}`：删除账户
*   `POST /api/accounts/{id}/ban`：禁用账户
*   `POST /api/accounts/{id}/unban`：解禁账户
*   `GET /api/settings`：获取设置
*   `POST /api/settings`：更新设置

## 📝 注意事项

*   **安全性**：请务必在部署后立即修改默认的 `ADMIN_PASSWORD`。
*   **数据库**：数据存储在本地 `dreamina.db` SQLite 数据库中，请定期备份。
*   **Session ID**：请确保添加的 Session ID 有效且格式正确。

## 📄 License

MIT License
