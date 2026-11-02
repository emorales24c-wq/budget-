"""Real data: PumpPortal + Raydium logs (discovery), Solana RPC + RugCheck (safety),
DexScreener (market), Jupiter (quotes and live swaps).

Third-party response schemas drift. Every parser here is defensive and returns None
on anything unexpected, which the Filter turns into a rejection. Run paper mode first
and read the rejection log: if *everything* is rejected for "unverifiable", a schema
moved and needs a fix here, not a looser filter.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import AsyncIterator

import httpx

from ..config import Endpoints
from ..models import SOL_MINT, Candidate, MarketSnapshot
from .base import Quote, TokenSafety

log = logging.getLogger("desk.sources")

RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RUG_FLAG_WORDS = ("rug", "creator history", "previous", "scam")


class SolanaGateway:
    def __init__(self, ep: Endpoints, rugger_file: str = "data/rugged_devs.txt"):
        self.ep = ep
        self.http = httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "memedesk/1.0"})
        self._decimals: dict[str, int] = {}
        self._rugger_file = Path(rugger_file)
        self.known_ruggers: set[str] = set()
        if self._rugger_file.exists():
            self.known_ruggers = {l.strip() for l in self._rugger_file.read_text().splitlines()
                                  if l.strip() and not l.startswith("#")}

    def remember_rugger(self, wallet: str) -> None:
        if wallet and wallet not in self.known_ruggers:
            self.known_ruggers.add(wallet)
            self._rugger_file.parent.mkdir(parents=True, exist_ok=True)
            with self._rugger_file.open("a") as f:
                f.write(wallet + "\n")

    async def close(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------------ discovery
    async def discover(self) -> AsyncIterator[Candidate]:
        q: asyncio.Queue[Candidate] = asyncio.Queue(maxsize=1000)
        tasks = [asyncio.create_task(self._pumpportal(q)),
                 asyncio.create_task(self._raydium_logs(q))]
        try:
            while True:
                yield await q.get()
        finally:
            for t in tasks:
                t.cancel()

    async def _pumpportal(self, q: asyncio.Queue) -> None:
        import websockets
        while True:
            try:
                async with websockets.connect(self.ep.pumpportal_ws, ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    async for raw in ws:
                        msg = json.loads(raw)
                        mint = msg.get("mint")
                        if not mint:
                            continue
                        src = "pumpfun-migrated" if msg.get("txType") == "migrate" else "pumpfun"
                        q.put_nowait(Candidate(mint=mint, source=src, symbol=msg.get("symbol", "?"),
                                               creator=msg.get("traderPublicKey")))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # reconnect forever, with backoff
                log.warning("pumpportal ws: %s", e)
                await asyncio.sleep(5)

    async def _raydium_logs(self, q: asyncio.Queue) -> None:
        import websockets
        while True:
            try:
                async with websockets.connect(self.ep.rpc_ws, ping_interval=20, max_size=2**22) as ws:
                    for i, prog in enumerate((RAYDIUM_AMM_V4, RAYDIUM_CPMM)):
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": i + 1, "method": "logsSubscribe",
                                                  "params": [{"mentions": [prog]}, {"commitment": "confirmed"}]}))
                    async for raw in ws:
                        val = json.loads(raw).get("params", {}).get("result", {}).get("value", {})
                        logs = val.get("logs") or []
                        if val.get("err") or not any("initialize2" in l or "Instruction: Initialize" in l for l in logs):
                            continue
                        mint = await self._mint_from_tx(val.get("signature"))
                        if mint:
                            q.put_nowait(Candidate(mint=mint, source="raydium"))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("raydium logs ws: %s", e)
                await asyncio.sleep(5)

    async def _mint_from_tx(self, sig: str | None) -> str | None:
        if not sig:
            return None
        tx = await self._rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                                      "maxSupportedTransactionVersion": 0}])
        try:
            mints = {b["mint"] for b in tx["meta"]["postTokenBalances"]} - {SOL_MINT}
            return next(iter(mints)) if len(mints) == 1 else None
        except (TypeError, KeyError):
            return None

    # ------------------------------------------------------------------ safety
    async def _rpc(self, method: str, params: list):
        try:
            r = await self.http.post(self.ep.rpc_http, json={"jsonrpc": "2.0", "id": 1,
                                                             "method": method, "params": params})
            r.raise_for_status()
            return r.json().get("result")
        except Exception as e:
            log.warning("rpc %s: %s", method, e)
            return None

    async def _get_json(self, url: str):
        try:
            r = await self.http.get(url)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("GET %s: %s", url, e)
            return None

    async def safety(self, mint: str) -> TokenSafety:
        acct, report = await asyncio.gather(
            self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}]),
            self._get_json(f"{self.ep.rugcheck}/tokens/{mint}/report"),
        )
        # Authorities straight from chain (source of truth, not a third party).
        mint_rev = freeze_rev = None
        decimals = None
        try:
            info = acct["value"]["data"]["parsed"]["info"]
            mint_rev = info.get("mintAuthority") is None
            freeze_rev = info.get("freezeAuthority") is None
            decimals = int(info["decimals"])
            self._decimals[mint] = decimals
        except (TypeError, KeyError, ValueError):
            pass

        lp_pct = top10 = None
        creator = None
        flags: list[str] = []
        checked = False
        rugged = False
        notes: list[str] = []
        if isinstance(report, dict):
            creator = report.get("creator")
            rugged = bool(report.get("rugged"))
            markets = report.get("markets") or []
            pcts = [m.get("lp", {}).get("lpLockedPct") for m in markets if isinstance(m.get("lp"), dict)]
            pcts = [float(p) for p in pcts if p is not None]
            lp_pct = min(pcts) if pcts else None   # worst market decides
            # vaults/pools are not "holders"
            pool_accounts = set()
            for m in markets:
                for k in ("liquidityAAccount", "liquidityBAccount", "pubkey"):
                    if m.get(k):
                        pool_accounts.add(m[k])
            known = report.get("knownAccounts") or {}
            amm_like = {a for a, v in known.items() if isinstance(v, dict) and v.get("type") in ("AMM", "LOCKER")}
            holders = report.get("topHolders")
            if isinstance(holders, list):
                real = [h for h in holders
                        if h.get("address") not in pool_accounts
                        and h.get("owner") not in amm_like and h.get("address") not in amm_like]
                top10 = sum(float(h.get("pct") or 0) for h in real[:10])
            risks = report.get("risks")
            if isinstance(risks, list) and creator:
                checked = True
                for r in risks:
                    name = str(r.get("name", "")).lower()
                    if "creator" in name and any(w in name for w in RUG_FLAG_WORDS):
                        flags.append(r.get("name"))
        else:
            notes.append("rugcheck report unavailable")

        if creator and creator in self.known_ruggers:
            flags.append("creator in local rugger blacklist")
            checked = True

        return TokenSafety(mint_authority_revoked=mint_rev, freeze_authority_revoked=freeze_rev,
                           lp_locked_pct=lp_pct, top10_pct=top10, creator=creator,
                           creator_rug_flags=tuple(flags), creator_history_checked=checked,
                           already_rugged=rugged, decimals=decimals, notes=tuple(notes))

    # ------------------------------------------------------------------ market
    async def market(self, mint: str) -> MarketSnapshot | None:
        pairs = await self._get_json(f"{self.ep.dexscreener}/token-pairs/v1/solana/{mint}")
        if not isinstance(pairs, list) or not pairs:
            return None
        sol_pairs = [p for p in pairs if (p.get("quoteToken") or {}).get("address") == SOL_MINT
                     and (p.get("baseToken") or {}).get("address") == mint]
        if not sol_pairs:
            return None
        p = max(sol_pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
        try:
            txh1 = (p.get("txns") or {}).get("h1") or {}
            created = p.get("pairCreatedAt")
            return MarketSnapshot(
                price_usd=float(p["priceUsd"]), price_sol=float(p["priceNative"]),
                liquidity_usd=float(p["liquidity"]["usd"]), volume_1h_usd=float(p["volume"]["h1"]),
                buys_1h=int(txh1.get("buys", 0)), sells_1h=int(txh1.get("sells", 0)),
                price_change_1h_pct=float((p.get("priceChange") or {}).get("h1") or 0),
                price_change_5m_pct=float((p.get("priceChange") or {}).get("m5") or 0),
                pair_age_min=(time.time() * 1000 - created) / 60000 if created else 0.0,
                fdv_usd=float(p.get("fdv") or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ quotes
    async def _decimals_for(self, mint: str) -> int | None:
        if mint not in self._decimals:
            await self.safety(mint)
        return self._decimals.get(mint)

    async def _quote(self, in_mint: str, out_mint: str, amount_raw: int, slippage_bps: int) -> dict | None:
        url = (f"{self.ep.jupiter}/quote?inputMint={in_mint}&outputMint={out_mint}"
               f"&amount={amount_raw}&slippageBps={slippage_bps}")
        q = await self._get_json(url)
        return q if isinstance(q, dict) and "outAmount" in q else None

    @staticmethod
    def _impact_pct(q: dict) -> float:
        # Treated as a fraction and scaled to %. If the API ever reports percent
        # instead, this overstates impact -> more rejections, never fewer.
        try:
            return abs(float(q.get("priceImpactPct", 1))) * 100.0
        except (TypeError, ValueError):
            return 100.0

    async def quote_buy(self, mint: str, sol_in: float, slippage_bps: int) -> Quote | None:
        dec = await self._decimals_for(mint)
        if dec is None:
            return None
        q = await self._quote(SOL_MINT, mint, int(sol_in * 1e9), slippage_bps)
        if not q:
            return None
        # use otherAmountThreshold = worst case fill under slippage, not the optimistic outAmount
        worst = int(q.get("otherAmountThreshold") or q["outAmount"])
        return Quote(sol_in, worst / 10 ** dec, self._impact_pct(q), q)

    async def quote_sell(self, mint: str, tokens: float, slippage_bps: int) -> Quote | None:
        dec = await self._decimals_for(mint)
        if dec is None:
            return None
        q = await self._quote(mint, SOL_MINT, int(tokens * 10 ** dec), slippage_bps)
        if not q:
            return None
        worst = int(q.get("otherAmountThreshold") or q["outAmount"])
        return Quote(tokens, worst / 1e9, self._impact_pct(q), q)

    # ------------------------------------------------------------------ live swaps
    async def swap(self, quote: Quote, keypair) -> str | None:
        """Sign and send a Jupiter swap. Returns the signature once confirmed, else None."""
        import base64
        from solders.transaction import VersionedTransaction

        try:
            r = await self.http.post(f"{self.ep.jupiter}/swap", json={
                "quoteResponse": quote.raw, "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto"})
            r.raise_for_status()
            raw = base64.b64decode(r.json()["swapTransaction"])
            unsigned = VersionedTransaction.from_bytes(raw)
            signed = VersionedTransaction(unsigned.message, [keypair])
            sig = await self._rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                                      {"encoding": "base64", "skipPreflight": False,
                                                       "maxRetries": 3}])
        except Exception as e:
            log.error("swap failed: %s", e)
            return None
        if not sig:
            return None
        for _ in range(40):  # ~60s
            st = await self._rpc("getSignatureStatuses", [[sig]])
            v = (st or {}).get("value", [None])[0] if st else None
            if v and v.get("err"):
                return None
            if v and v.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig
            await asyncio.sleep(1.5)
        return None

    async def sol_balance(self, pubkey: str) -> float | None:
        r = await self._rpc("getBalance", [pubkey])
        return r["value"] / 1e9 if r else None

    async def token_balance(self, owner: str, mint: str) -> float | None:
        r = await self._rpc("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        try:
            return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                       for a in r["value"])
        except (TypeError, KeyError):
            return None
