# mimo-usage

platform.xiaomimimo.com（小米 MiMo 开放平台）用量数据的**只读**包装服务。
Sanic + httpx + orjson + uvloop。

- 协议说明见源码注释（`mimo_usage/client.py`）与测试（`tests/`）

> **免责声明与合规提示**
>
> - 本项目**非官方**，与小米 / MiMo 无任何关联，未获其授权或认可。
> - 它包装的是网页端内部接口，**可能违反上游服务条款**；请仅用于**学习研究与个人自用**，
>   自行评估并承担全部风险（包括账号被限流、封禁等后果）。
> - 凭据（Cookie / passToken / 密码派生值）由使用者自备，**只应保存在本机**；请勿公开分享
>   或提交到仓库。默认不开启访问密钥（`MIMO_API_KEY` 留空），**不要将服务暴露到公网**。
> - 仅实现**只读**用量查询，不包含购买、取消、导出、团队等任何写操作。
> - 软件按"现状"提供，不提供任何明示或暗示的担保。

## 功能

- `/healthz` 存活探针 + 凭证/续登状态
- `/dashboard` 无外链看板：两排 stats、SVG 用量折线、月账单柱图（图表禁选、数据区可复制、静态资源 `Cache-Control` + `?v=`）
- 只读 API：
  - `GET /api/v1/summary` — 套餐 / 月额度 / 计划额度 / 余额 / token·费用 概览
  - `GET /api/v1/usage` — 账户维度 token/费用/限速
  - `GET /api/v1/usage/detail?year=&month=` — 年（月）用量明细
  - `GET /api/v1/usage/bill` — 月账单
  - `GET /api/v1/token-plan` — 订阅详情 + 额度用量 + 可购套餐
  - `GET /api/v1/account` — userProfile + balance + projects
  - `GET /api/v1/overview` — 以上并发聚合（`?fields=` 选段）
- Cookie 鉴权（`api-platform_serviceToken` 等），凭证文件 **mtime 热重载**
- 401/loginUrl 自动续登：`serviceLoginAuth2 → /sts` 换新 serviceToken 落盘；
  失败进入冷却期返回可读错误，不循环打上游
- TTL 缓存 + single-flight、`?refresh=1` 限流、stale 兜底（鉴权错误不吃 stale）
- 上游字段兼容层（`mimo_usage/compat.py`）

**不做**：购买/取消/导出/team 等任何写操作。

## 快速开始

```bash
# 1) 集中配置：复制模板并填写（含凭据；git 已忽略，记得 chmod 600）
cp config.yaml.example config.yaml
chmod 600 config.yaml
vim config.yaml   # 至少填 credentials.serviceToken/deviceId/userId；
                  # 可选：passToken（30 天免密根）、username + password（密码续登兜底）

# 2) 启动（存在 config.yaml 时 run.sh 自动 export MIMO_CONFIG）
./run.sh          # 或 MIMO_CONFIG=./config.yaml python3 -m mimo_usage
```

打开 <http://127.0.0.1:8000/dashboard>；API 示例：

```bash
curl -s localhost:8000/api/v1/summary | jq .data
curl -s 'localhost:8000/api/v1/usage/detail?year=2026&month=9' | jq .
curl -s localhost:8000/api/v1/overview | jq '.meta.errors'
```

### 凭据与自动续登（三级降级）

```
serviceToken 过期(401)
 ├─ ① serviceLogin 免密直通（passToken 有效期内，全自动）
 ├─ ② serviceLoginAuth2 passToken 免密交换
 └─ ③ 密码登录（username + passwordMd5 齐备时兜底；成功顺带刷新 passToken 自愈）
```

- **密码读后即焚**：`credentials.password` 明文只写一次，首次加载即变为
  `passwordMd5` + `passwordBcrypt`（`$2b$10$…`）并从文件删除，`chmod 600`
- 换密码：再写一次 `password` 启动即可（自动重算/删除）
- 防验证码：沿用**同一浏览器**的 `deviceFingerprint`/`deviceId`、Chrome 拟态头、密码失败
  1h 冷却、`captchaUrl` 出现立即熔断（实现见 `mimo_usage/client.py`）
- 续登换发的 serviceToken/ph/slh/userId/passToken **原子写回 config.yaml**

环境变量仍全部可用且**优先级高于 config.yaml**（`MIMO_CONFIG` 指定文件路径；
`MIMO_PASSWORD` 明文只进内存并落 md5/bcrypt，绝不写入文件）。旧 `secrets/*_FILE`
文件后端保留兼容。

### 访问密钥（可选）

**不配 `MIMO_API_KEY` 时完全不鉴权**（默认形态，适合本机/内网）。配了之后：

| 途径 | 适用 | 说明 |
|---|---|---|
| `X-API-Key: <key>` 请求头 | 脚本 / 前端 JS | API 的首选方式 |
| `?key=<key>` query | 浏览器首次打开看板 | 命中后种下 `mimo_usage_key` cookie（`Path=/dashboard`，HttpOnly，30 天） |
| `mimo_usage_key` cookie | 看板 HTML/JS/CSS | 只覆盖 `/dashboard*`；**不会发往 `/api/*`**（故 API 必须走请求头） |

浏览器用法：访问 `/dashboard?key=你的密钥` **一次**——前端会把密钥存进 `localStorage`
并立刻从地址栏抹掉，之后 API 请求自动带 `X-API-Key`；换密钥后需重新打开一次。
`/healthz` 不受密钥限制（探针）。

> 拒绝语义：什么都没带 → 403 `forbidden`；带了不匹配 → 401 `unauthorized`（body 泛化，不泄露认证方式）。

## 配置（节选）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MIMO_CONFIG` | — | config.yaml 路径（`./run.sh` 在文件存在时自动导出） |
| `MIMO_BASE_URL` | `https://platform.xiaomimimo.com` | 用量 API 基址 |
| `MIMO_ACCOUNT_BASE_URL` | `https://account.xiaomi.com` | 续登基址 |
| `MIMO_SERVICE_TOKEN` / `MIMO_PASS_TOKEN` / `MIMO_DEVICE_ID` | — | 覆盖 yaml 对应凭据（env > yaml） |
| `MIMO_USERNAME` / `MIMO_PASSWORD` / `MIMO_PASSWORD_HASH` | — | 密码续登材料（明文仅内存，自动转 md5+bcrypt） |
| `MIMO_DEVICE_FINGERPRINT` | 无（必填才可密码续登） | 浏览器登录时抓取的 FingerprintJS visitorId |
| `MIMO_API_KEY` | 空=不鉴权 | `X-API-Key` / `?key=` / dashboard cookie |
| `MIMO_REAUTH_COOLDOWN` | `300` | 免密续登失败冷却（秒） |
| `MIMO_PASSWORD_REAUTH_COOLDOWN` | `3600` | 密码分支失败/验证码冷却（秒） |
| `MIMO_PORT` / `MIMO_HOST` / `MIMO_WORKERS` | `8000` / `0.0.0.0` / `1` | 服务 |

完整列表：[config.yaml.example](./config.yaml.example)（主）与 [.env.example](./.env.example)（env 形态）。

## 开发

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest -q      # MockTransport：code=0 / 401 / 免密·密码续登 / captcha / yaml 迁移
ruff check .
node --check mimo_usage/static/app.js
node --check mimo_usage/_crypto_node.cjs
```

## 目录

```
mimo_usage/
  config.py           # env > config.yaml > 默认；YamlStore（原子写回/0600）；密码读后即焚迁移
  client.py           # 上游调用、code==0、三级续登（免密→Auth2→密码）+ captcha 熔断
  _crypto_node.cjs    # 密码登录参数加密（AES-CBC/RSA/MD5，复刻前端 encryptAes）
  cache.py            # TTLCache（single-flight/stale）+ IntervalGate
  timerange.py        # Asia/Shanghai 时间窗 + year/month 稳定窗口
  compat.py           # 上游字段兼容层
  api/                # /api/v1 蓝图（sections/summary/overview/system）
  static/             # 看板（无外链，纯手写 DOM/SVG）
config.yaml.example   # 主配置模板（复制为 config.yaml，git 忽略）
tests/                # pytest + httpx.MockTransport
```

