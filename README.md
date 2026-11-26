# 缓存陈旧故障步骤回放台

源站换版后代理仍返回旧构建号时，值班员把「换版 / 查代理 / 推进时钟」的操作记录贴进页面，
系统从**同一初态**回放，并自动把故障步骤缩短到最小，交给缓存维护者。

## 实现概览

```
app/
├── __init__.py
├── web.py                 # Flask：页面、/api/replay、/demo/* 内置源站与代理演示
├── templates/index.html   # 单页（原生 JS，无外部 CDN 依赖）
└── replay/
    ├── engine.py          # 虚拟时钟、本地源站 Origin、带 TTL 的缓存代理 CacheProxy、
    │                      # 记录解析 parse_record、单轮回放 replay_record
    ├── minimize.py        # ddmin 式记录缩短 + 回放次数预算 + 单删核验
    └── errors.py
tests/                     # pytest：引擎 17 项 + 缩短 9 项 + HTTP 8 项
Dockerfile                 # base（运行）/ test（自测）两个 stage
docker-compose.yml         # replay-web 服务 + replay-selftest 一次性自测服务
```

### 核心语义

- **同一初态**：每次回放都新建虚拟时钟（从 0 开始）、源站（初态版本可配，默认 `build-1`）
  和代理（TTL 可配，默认 30 秒），互不污染，结果完全确定可复现。
- **虚拟时钟**：只有执行「推进时钟」才前进；TTL 判定为 `当前时刻 >= 写入时刻 + TTL` 即过期回源。
- **末步固定查询**：解析时强制最后一步必须是 `查代理 / query`，否则报错。
- **目标版本对**：先完整回放原记录，在轨迹中找第一次「代理版本 ≠ 源站版本」的查询，
  以该 `(源站版本, 代理版本)` 作为缩短后末步必须复现的目标对。
- **先跑原记录**：若整条轨迹从未读到旧版，直接报出末步两边版本、状态置 `not_reproduced` 并停止，不做缩短。
- **缩短算法**：末步查询保留不动，对前置操作做 Zeller ddmin 贪心缩减；每删一段都从同一初态
  重新回放，末步版本对与目标对一致才算复现。
- **精简完毕口径**：算法细化到单元素粒度，且单独删掉当前记录的**任一**前置操作都无法复现，
  才标记 `minimal`。
- **次数上限**：表单可设回放次数上限（含原记录首跑，默认 100）。预算耗尽仍未核完时状态置
  `unverified`，给出截至目前已复现的最短记录（若一次成功缩短都没来得及做，就把原记录截断到
  第一次读到旧版的查询步兜底），并明确标明未核完与剩余预算。

### 操作记录格式

每行一步，中文或英文关键字均可，也支持 JSON 行；空行与 `#`、`//` 注释行会忽略。

```
查代理
换版 build-2
推进时钟 20
查代理
```

| 操作 | 别名 | 参数 |
| --- | --- | --- |
| 换版 | deploy / publish / 部署 / 发布 | 可选版本号；缺省自动递增 build-2、build-3… |
| 查代理 | query / get / 查询 | 无 |
| 推进时钟 | advance / sleep / tick / 推进 / 等待 | 正整数秒 |

## 用 Docker Compose 启动

```bash
# 启动 Web（http://localhost:8000）
docker compose up -d --build

# 容器内一键自测
docker compose --profile test run --rm replay-selftest
```

无 Docker 时的本地等价方式：

```bash
pip install -r requirements.txt pytest
PYTHONPATH=. pytest -v tests
gunicorn --bind=0.0.0.0:8000 --workers=1 app.web:app
```

## 实测结果

环境：Python 3.11.2、Flask 3.1.3、pytest 9.2.0、gunicorn 23.0.0。

- `pytest -v tests`：**34 passed**（解析与参数校验 8、缓存/回放语义 9、缩短算法 9、HTTP 8）。
- gunicorn 实跑 + curl 端到端：
  - 示例 6 步记录（TTL=30）→ 状态 `minimal`，7 次回放，最短记录 3 步：
    `查代理 → 换版 build-2 → 查代理`，末步版本对 `build-2 / build-1` 与目标对一致。
    （换版后缓存未失效，立即查询即可见旧构建号，因此连「推进时钟」都被证明可删。）
  - 回放上限设 1 → 状态 `unverified`、`exhausted=true`，兜底截断为 4 步且末步仍复现 `build-2 / build-1`。
  - 推进到 TTL 外再查 → 状态 `not_reproduced`，报出版本对 `build-2 / build-2`，仅 1 次回放即停止。
  - 末步非查询等非法输入 → HTTP 400 并返回中文错误信息。
  - `/demo/*` 手动演示：回源 → 换版 → 20s 后命中旧缓存（build-1）→ 再 10s 到 TTL 边界回源（build-2）。

## 余项 / 边界说明

- 本机未安装 Docker daemon，Compose 文件仅做了 YAML 结构静态核对，未在本机实际 `docker compose build`；
  镜像内自测入口（`replay-selftest`）已就绪，需在有 Docker 的环境执行验证。
- `/demo/*` 的状态是进程内状态，因此 gunicorn 固定 `--workers=1`；回放接口本身无状态、每次从初态重建，不受影响。
- 页面为服务端渲染 + 原生 fetch，无前端构建与外部网络依赖，离线内网可用。
