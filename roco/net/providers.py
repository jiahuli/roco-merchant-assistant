"""数据渠道（Provider）层：从哪拿数据、按什么顺序降级。

职责边界（1.9 重构后）
----------------------
* 本模块只关心**渠道**：HTTP 抓取（经 netdirect 直连）、渠道适配、自动降级顺序；
* 页面/JSON 的解析规则在 `roco.net.parsing`（纯函数，可单测）；
* 轮次/营业时间的计算在 `roco.core.rounds`（全项目唯一的时间事实来源）。

设计要点
--------
* 官方接口优先：`official` 渠道默认地址为空，一旦用户填入官方地址就会最先被调用；
  官方不提供该接口时自动跳过，不会阻塞。
* 地址可替换：所有 base_url 都来自 config.json，界面里可直接改。
* 多渠道切换：界面下拉框切换；勾选"自动降级"时，当前渠道失败会按顺序尝试下一个。
* 结构自适应：真实世界里的接口字段五花八门，解析层用 candidate-key 的方式做映射，
  因此接「自定义接口」时不必改代码。
"""
from __future__ import annotations

import json
from typing import Any

from roco.config import Config
from roco.core.models import MerchantSnapshot
from roco.net import netdirect
from roco.net.parsing import (_find_items, _parse_dt, build_items,
                              parse_bwiki_shops, parse_onebiji_html,
                              snapshot_from_items, snapshot_from_payload)
from roco.core.rounds import (STATUS_CLOSED, STATUS_OPEN, is_open_at, next_refresh_at,
                              now_cn, round_index_at)


# ---------------------------------------------------------------------------
# 渠道基类
# ---------------------------------------------------------------------------
class ProviderError(RuntimeError):
    pass


class BaseProvider:
    id = "base"
    default_label = "渠道"

    def __init__(self, conf: dict[str, Any], timeout: int = 15) -> None:
        self.conf = conf or {}
        self.timeout = timeout
        self.route: dict[str, Any] = {}

    # -- 便于子类读取 --
    @property
    def base_url(self) -> str:
        return str(self.conf.get("base_url") or "").strip()

    @property
    def api_key(self) -> str:
        return str(self.conf.get("api_key") or "").strip()

    @property
    def label(self) -> str:
        return str(self.conf.get("label") or self.default_label)

    @property
    def preferred_ip(self) -> str:
        """手动指定的建连 IP（绕过 DNS/CDN 归属不理想时用）。"""
        return str(self.conf.get("preferred_ip") or "").strip()

    def is_configured(self) -> bool:
        return bool(self.base_url)

    # -- HTTP --
    def _request(self, url: str, headers: dict[str, str] | None = None,
                 use_doh: bool = True) -> Any:
        """直连请求：不走任何代理，DoH 取真实 IP，TLS 校验照旧。

        路由信息挂在 `self.route` 上，供界面显示"出口是否被代理接管"。
        """
        merged = {
            "User-Agent": "RocoMerchantAssistant/1.6 (+desktop; direct-tls)",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if headers:
            merged.update(headers)
        try:
            payload, trace = netdirect.fetch_json(
                url, merged, timeout=self.timeout,
                use_doh=use_doh, prefer_ip=self.preferred_ip,
            )
        except netdirect.DirectError as exc:
            raise ProviderError(str(exc)) from exc
        self.route = trace
        return payload

    def _request_text(self, url: str, headers: dict[str, str] | None = None,
                      use_doh: bool = True) -> str:
        """抓 HTML（页面即数据源的渠道用）。

        与 `_request` 共用同一套直连 / DoH / 代理剥离逻辑，
        只是不改 JSON 解析这一步。
        """
        merged = {
            "User-Agent": "RocoMerchantAssistant/1.6 (+desktop; direct-tls)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if headers:
            merged.update(headers)
        try:
            body, trace = netdirect.fetch_bytes(
                url, merged, timeout=self.timeout, use_doh=use_doh,
            )
        except netdirect.DirectError as exc:
            raise ProviderError(str(exc)) from exc
        self.route = trace
        return body.decode("utf-8", errors="replace")

    def fetch(self) -> MerchantSnapshot:  # pragma: no cover - 由子类实现
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 官方渠道：结构未知，交给通用解析；地址由用户填写
# ---------------------------------------------------------------------------
class OfficialProvider(BaseProvider):
    id = "official"
    default_label = "官方公开 API"

    def fetch(self) -> MerchantSnapshot:
        payload = self._request(self.base_url, self._headers())
        return snapshot_from_payload(payload, self.id, self.label)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


# ---------------------------------------------------------------------------
# 中文社区数据：每日远行商人查询器（页面本身就是数据源）
# ---------------------------------------------------------------------------
class OnebijiProvider(BaseProvider):
    """中文社区数据：每日远行商人查询器。

    页面本身就是数据源（没有 JSON API），解析规则见 `parsing.parse_onebiji_html`；
    站点一旦改版，解析会得到空列表并抛错，让上层降级到下一个渠道——
    绝不把"没解析到"当成"本轮没有商品"照常渲染。
    """

    id = "onebiji"
    default_label = "社区数据 · 每日商人查询器（中文）"

    def fetch(self) -> MerchantSnapshot:
        page = self._request_text(self.base_url, {"Referer": "https://www.onebiji.com/"})
        items = parse_onebiji_html(page)
        if not items:
            raise ProviderError("页面里没有解析到商品（站点可能改版或被网关拦截）")
        return snapshot_from_items(items, self.id, self.label,
                                   "数据解析自中文查询器页面，按轮次筛选")


# ---------------------------------------------------------------------------
# 哔哩哔哩 Wiki：只有商店目录，没有每轮货架
# ---------------------------------------------------------------------------
class BwikiProvider(BaseProvider):
    """哔哩哔哩 BWIKI（可选渠道，默认不填地址）。

    它只有「商店一览」目录：不列价格、没有轮次与刷新时间，商品行也不与商店归组。
    因此本渠道**主动**给出明确错误，而不是返回一份看起来像货架的空/全量数据。
    """

    id = "bwiki"
    default_label = "哔哩哔哩 Wiki · 商店目录（无货架）"

    def fetch(self) -> MerchantSnapshot:
        payload = self._request(self.base_url)
        page = ""
        if isinstance(payload, dict):
            page = str((payload.get("parse") or {}).get("text") or "")
        if not page:
            raise ProviderError("返回结构异常：不是 MediaWiki 的 parse 结果")
        shops = parse_bwiki_shops(page)
        hits = [(name, count) for name, count in shops if "远行商人" in name]
        if not hits:
            raise ProviderError("商店目录里没有「远行商人」，Wiki 结构可能已改版")
        raise ProviderError(
            f"BWIKI 只有商店目录（{hits[0][0]} · {hits[0][1]} 件商品），"
            "不含价格 / 轮次 / 每轮货架，无法作为货架数据源；请改用中文查询器渠道"
        )


# ---------------------------------------------------------------------------
# 社区精简：只有商品名
# ---------------------------------------------------------------------------
class YxsrProvider(BaseProvider):
    id = "yxsr"
    default_label = "社区精简 · 远行商人资源卡"

    def fetch(self) -> MerchantSnapshot:
        payload = self._request(self.base_url)
        items = build_items(_find_items(payload))
        moment = now_cn()
        refresh_stamp = None
        if isinstance(payload, dict):
            refresh_stamp = _parse_dt(payload.get("updateTimeBeijing"))
        return MerchantSnapshot(
            items=items,
            source_id=self.id,
            source_label=self.label,
            status=STATUS_OPEN if is_open_at(moment) else STATUS_CLOSED,
            round_index=round_index_at(moment),
            started_at=refresh_stamp.strftime("%Y-%m-%d %H:%M") if refresh_stamp else None,
            next_refresh=next_refresh_at(moment).strftime("%Y-%m-%d %H:%M"),
            fetched_at=moment,
            message="该渠道不含价格与图标",
        )


# ---------------------------------------------------------------------------
# 洛克魔法书：需自带 Key
# ---------------------------------------------------------------------------
class ShallowProvider(BaseProvider):
    id = "shallow"
    default_label = "洛克魔法书（需 Key）"

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def fetch(self) -> MerchantSnapshot:
        if not self.api_key:
            raise ProviderError("未配置 API Key，请到「设置 → 数据渠道」填写")
        payload = self._request(self.base_url, {"Authorization": f"Bearer {self.api_key}"})
        snapshot = snapshot_from_payload(payload, self.id, self.label)
        if not snapshot.items:
            raise ProviderError("接口返回为空（常见原因：Key 无效或额度用尽）")
        return snapshot


# ---------------------------------------------------------------------------
# 自定义接口 / 大陆反代
# ---------------------------------------------------------------------------
class CnMirrorProvider(BaseProvider):
    """自建的大陆反向代理：把社区镜像的数据转发到国内服务器上。

    目标 IP 落在中国大陆，路由器端分流规则会放行直连，流量不经过 VPN 节点；
    同时链路只有 PC → 国内服务器 → Cloudflare，少一跳跨境。
    """

    id = "cn_mirror"
    default_label = "大陆反代 · 自建"

    def fetch(self) -> MerchantSnapshot:
        body = self._request_text(self.base_url)
        # 反代目标有两种：JSON（聚合接口）与页面（中文查询器）。
        # 先按 JSON 试，失败再按页面解析——两种都支持，配置才不用二选一。
        try:
            payload = json.loads(body.lstrip("\ufeff").strip())
        except (json.JSONDecodeError, ValueError):
            items = parse_onebiji_html(body)
            if not items:
                raise ProviderError(
                    "反代返回的既不是 JSON、也解析不出页面商品（是否只转发了首页？）") from None
            return snapshot_from_items(items, self.id, self.label,
                                       "数据解析自反代页面，按轮次筛选")
        snapshot = snapshot_from_payload(payload, self.id, self.label)
        if not snapshot.items:
            raise ProviderError("未能从返回值里识别出商品列表（反代是否只转发了首页？）")
        return snapshot


class CustomProvider(BaseProvider):
    id = "custom"
    default_label = "自定义接口"

    def fetch(self) -> MerchantSnapshot:
        headers: dict[str, str] = {}
        raw_headers = self.conf.get("headers") or ""
        if isinstance(raw_headers, dict):
            headers.update({str(k): str(v) for k, v in raw_headers.items()})
        elif isinstance(raw_headers, str) and raw_headers.strip():
            try:
                parsed = json.loads(raw_headers)
                if isinstance(parsed, dict):
                    headers.update({str(k): str(v) for k, v in parsed.items()})
            except json.JSONDecodeError as exc:
                raise ProviderError("自定义请求头不是合法 JSON") from exc
        if self.api_key:
            headers.setdefault("Authorization", f"Bearer {self.api_key}")

        payload = self._request(self.base_url, headers)
        snapshot = snapshot_from_payload(payload, self.id, self.label)
        if not snapshot.items:
            raise ProviderError("未能从返回值里识别出商品列表")
        return snapshot


# ---------------------------------------------------------------------------
# 工厂 + 自动降级
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, type[BaseProvider]] = {
    "official": OfficialProvider,
    "cn_mirror": CnMirrorProvider,
    "onebiji": OnebijiProvider,
    "bwiki": BwikiProvider,
    "yxsr": YxsrProvider,
    "shallow": ShallowProvider,
    "custom": CustomProvider,
}


def build_provider(pid: str, config: Config) -> BaseProvider:
    cls = _REGISTRY.get(pid, CustomProvider)
    return cls(config.provider_conf(pid), timeout=config.request_timeout)


def available_providers(config: Config) -> list[tuple[str, str, bool]]:
    """返回 (id, 名称, 是否已配置)。"""
    result = []
    for pid in config.provider_order():
        provider = build_provider(pid, config)
        result.append((pid, provider.label, provider.is_configured()))
    return result


_REGION_RANK = {"cn": 0, "": 1, "overseas": 2}


def _region_rank(config: Config, pid: str) -> int:
    """区域排序权重：大陆 0 < 未知 1 < 境外 2。"""
    region = str(config.provider_conf(pid).get("region") or "")
    return _REGION_RANK.get(region, 1)


def provider_regions(config: Config) -> dict[str, str]:
    """供界面展示：渠道 id → 区域标签。"""
    return {
        pid: str(config.provider_conf(pid).get("region") or "")
        for pid in config.provider_order()
    }


def fetch_snapshot(config: Config) -> tuple[MerchantSnapshot, list[str]]:
    """按配置拉取数据，返回 (快照, 错误信息列表)。"""
    order: list[str] = []
    primary = config.provider
    order.append(primary)
    if config.auto_fallback:
        rest = [pid for pid in config.provider_order() if pid != primary]
        if config.prefer_cn_region:
            # 大陆渠道优先尝试：目标 IP 在境内时路由器不会转发到代理节点，链路更短更稳
            rest = sorted(rest, key=lambda pid: _region_rank(config, pid))
        order.extend(rest)

    errors: list[str] = []
    for pid in order:
        provider = build_provider(pid, config)
        if not provider.is_configured():
            if pid == primary:
                errors.append(f"{provider.label}：未配置接口地址")
            continue
        try:
            snapshot = provider.fetch()
        except ProviderError as exc:
            errors.append(f"{provider.label}：{exc}")
            continue
        except Exception as exc:  # 兜底，避免单渠道异常拖垮界面
            errors.append(f"{provider.label}：{type(exc).__name__} {exc}")
            continue
        if not snapshot.items and snapshot.status == STATUS_OPEN:
            errors.append(f"{provider.label}：本轮暂无商品数据")
            continue
        snapshot.source_id = pid
        snapshot.source_label = provider.label
        snapshot.route = dict(getattr(provider, "route", {}) or {})

        # 出口校验：多一次轻量请求，但能把"是否被代理接管"变成可测的事实
        if config.verify_egress:
            snapshot.route["egress"] = netdirect.egress_of(
                snapshot.route.get("host", ""), timeout=6
            ) if snapshot.route.get("host") else {"ok": False}
        return snapshot, errors

    raise ProviderError("\n".join(errors) if errors else "没有可用的数据渠道，请到设置里填写接口地址")
