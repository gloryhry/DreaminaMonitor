import json
import random
import httpx
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from fastapi import APIRouter, Request, HTTPException, Response, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
from database import get_db, Account, AsyncSessionLocal
from config import settings

logger = logging.getLogger(__name__)

# Commerce API URLs (参考 jimeng-api)
COMMERCE_URL_US = "https://commerce.us.capcut.com"
COMMERCE_URL_SG = "https://commerce-api-sg.capcut.com"  # HK/JP/SG 共用

# 伪装 Headers (参考 jimeng-api)
FAKE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Google Chrome";v="142", "Chromium";v="142", "Not_A Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
}

async def fetch_account_credit(session_id: str, region: str) -> Optional[float]:
    """
    获取账户积分信息。
    CN 区域不支持积分查询，返回 None。
    
    Args:
        session_id: 账户的 session_id（不含区域前缀）
        region: 账户区域 (us, hk, jp, sg, cn)
    
    Returns:
        总积分 (gift_credit + purchase_credit + vip_credit)，CN 区域返回 None
    """
    region_lower = region.lower()
    
    # CN 区域不支持积分查询
    if region_lower == "cn":
        logger.debug(f"[CreditQuery] 跳过 CN 区域账户积分查询")
        return None
    
    # 根据区域选择 Commerce API
    if region_lower == "us":
        base_url = COMMERCE_URL_US
        aid = 513641
    else:  # hk, jp, sg
        base_url = COMMERCE_URL_SG
        aid = 513641
    
    # 构造 Cookie
    cookie = f"sessionid={session_id}; sessionid_ss={session_id}; sid_tt={session_id}"
    
    # 构造请求头
    headers = {
        **FAKE_HEADERS,
        "Cookie": cookie,
        "Referer": "https://dreamina.capcut.com/",
        "Origin": "https://dreamina.capcut.com",
        "Content-Type": "application/json",
    }
    
    url = f"{base_url}/commerce/v1/benefits/user_credit"
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                json={},
                headers=headers,
                params={"aid": aid}
            )
            
            if response.status_code != 200:
                logger.warning(f"[CreditQuery] 积分查询失败: HTTP {response.status_code}")
                return None
            
            data = response.json()
            
            # 检查响应
            if data.get("ret") != "0" and data.get("ret") != 0:
                logger.warning(f"[CreditQuery] 积分查询返回错误: {data}")
                return None
            
            credit_data = data.get("data", {}).get("credit", {})
            gift_credit = credit_data.get("gift_credit", 0)
            purchase_credit = credit_data.get("purchase_credit", 0)
            vip_credit = credit_data.get("vip_credit", 0)
            
            total_credit = gift_credit + purchase_credit + vip_credit
            logger.info(f"[CreditQuery] 积分查询成功: 赠送={gift_credit}, 购买={purchase_credit}, VIP={vip_credit}, 总计={total_credit}")
            
            return float(total_credit)
            
    except Exception as e:
        logger.error(f"[CreditQuery] 积分查询异常: {e}")
        return None

router = APIRouter()

# 轮询索引
_round_robin_index = 0

async def get_valid_account(model_name: str, db: AsyncSession) -> Optional[Account]:
    """
    Select a valid account from the database.
    Criteria:
    - Not banned (or ban expired)
    - Usage count for the specific model is below threshold
    """
    now = datetime.now()
    
    # First, unban any accounts that have passed their ban time
    # We can do this lazily or via a background task. For simplicity, we query for non-banned OR ban_expired
    
    # Build query for valid accounts
    # We need to check specific model limits based on model_name
    # Mapping model name to field and limit
    model_field_map = {
        "jimeng-4.0": (Account.jimeng_4_0_count, settings.LIMIT_JIMENG_4_0),
        "jimeng-4.1": (Account.jimeng_4_1_count, settings.LIMIT_JIMENG_4_1),
        "nanobanana": (Account.nanobanana_count, settings.LIMIT_NANOBANANA),
        "nanobananapro": (Account.nanobananapro_count, settings.LIMIT_NANOBANANAPRO),
        "video-3.0": (Account.video_3_0_count, settings.LIMIT_VIDEO_3_0),
    }
    
    usage_condition = True
    if model_name in model_field_map:
        field, limit = model_field_map[model_name]
        usage_condition = field < limit
    
    # 非 nanobanana/nanobananapro 模型时，排除 points 为 0 的账户
    points_condition = True
    if model_name not in ["nanobanana", "nanobananapro"]:
        points_condition = Account.points > 0
    
    stmt = select(Account).where(
        and_(
            or_(Account.is_banned == False, Account.ban_until < now),
            Account.session_id.isnot(None),
            usage_condition,
            points_condition
        )
    )
    
    result = await db.execute(stmt)
    accounts = result.scalars().all()
    
    logger.info(f"[AccountSelect] 模型: {model_name}, 查询到 {len(accounts)} 个可用账户")
    
    if not accounts:
        return None
        
    # Round-robin selection for load balancing
    global _round_robin_index
    _round_robin_index = (_round_robin_index + 1) % len(accounts)
    return accounts[_round_robin_index]

async def update_account_usage(account_id: int, model_name: str):
    """
    Increment usage counter for the account and model.
    Also fetch and update account credit (skip CN region).
    """
    async with AsyncSessionLocal() as db:
        account = await db.get(Account, account_id)
        if not account:
            return
            
        if model_name == "jimeng-4.0":
            account.jimeng_4_0_count += 1
        elif model_name == "jimeng-4.1":
            account.jimeng_4_1_count += 1
        elif model_name == "nanobanana":
            account.nanobanana_count += 1
        elif model_name == "nanobananapro":
            account.nanobananapro_count += 1
        elif model_name == "video-3.0":
            account.video_3_0_count += 1
        
        # 查询并更新积分（CN 区域跳过）
        if account.session_id and account.region:
            credit = await fetch_account_credit(account.session_id, account.region)
            if credit is not None:
                account.points = credit
                logger.info(f"[CreditUpdate] {account.email} 积分已更新: {credit}")
            
        await db.commit()

async def ban_account_temp(account_id: int, hours: int = 4):
    """
    Temporarily ban an account.
    """
    async with AsyncSessionLocal() as db:
        account = await db.get(Account, account_id)
        if not account:
            return
            
        account.is_banned = True
        account.ban_until = datetime.now() + timedelta(hours=hours)
        account.error_count += 1
        await db.commit()

@router.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_request(request: Request, path: str, background_tasks: BackgroundTasks):
    # 1. Validate Authorization Header
    auth_header = request.headers.get("Authorization")
    logger.debug(f"Received request: {request.method} {path}")
    logger.debug(f"Headers: {request.headers}")
    
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.warning("Missing or invalid Authorization header")
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    token = auth_header.split(" ")[1]
    if token != settings.ADMIN_PASSWORD:
        logger.warning(f"Invalid API Key: {token}")
        raise HTTPException(status_code=401, detail="Invalid API Key")

    # 2. Extract Model Name (if POST/PUT and has body)
    model_name = "unknown"
    body_bytes = b""
    try:
        if request.method in ["POST", "PUT", "PATCH"]:
            body_bytes = await request.body()
            if body_bytes:
                try:
                    body_json = json.loads(body_bytes)
                    model_name = body_json.get("model", "unknown")
                    logger.debug(f"Extracted model name: {model_name}")
                    logger.debug(f"Request body: {body_json}")
                except json.JSONDecodeError:
                    logger.warning("Failed to decode JSON body")
                    pass
    except Exception as e:
        logger.error(f"Error reading body: {e}")
        pass

    # 3. Select Account
    async with AsyncSessionLocal() as db:
        account = await get_valid_account(model_name, db)
        
    if not account:
        logger.error(f"No available accounts for model: {model_name}")
        raise HTTPException(status_code=503, detail="No available accounts")
    
    logger.info(f"Selected account: {account.email} (Region: {account.region}, SessionID: {account.session_id})")

    # 4. Construct Upstream Request
    upstream_url = f"{settings.UPSTREAM_BASE_URL}/v1/{path}"
    logger.debug(f"Upstream URL: {upstream_url}")
    
    # Construct new Authorization token: "region-session_id"
    # Example: "us-eb5e141efa7f7a48deea298c80b7e620"
    new_token = f"{account.region}-{account.session_id}"
    
    # Prepare headers - filter out hop-by-hop headers and authorization
    # These headers should not be forwarded to upstream
    hop_by_hop_headers = {
        "host", "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailers", "transfer-encoding",
        "upgrade", "content-length", "accept-encoding", "authorization"
    }
    
    headers = {}
    for key, value in request.headers.items():
        if key.lower() not in hop_by_hop_headers:
            headers[key] = value
    
    # Set the new Authorization header with account credentials
    headers["Authorization"] = f"Bearer {new_token}"
    headers["Content-Type"] = "application/json"
    logger.debug(f"Forwarding headers: {headers}")
    
    # 5. Forward Request
    client = httpx.AsyncClient(timeout=settings.PROXY_TIMEOUT)
    try:
        response = await client.request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            content=body_bytes,
            params=request.query_params
        )
        
        # 6. Handle Response & Errors
        logger.info(f"Upstream response status: {response.status_code}")
        logger.debug(f"Upstream response body: {response.text[:500] if response.text else 'empty'}")
        
        if response.status_code in [429, 500, 524]:
            logger.warning(f"Banning account {account.id} due to error {response.status_code}")
            # Ban account logic
            background_tasks.add_task(ban_account_temp, account.id)
            # Return the error to the client
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=dict(response.headers)
            )
        
        # Success - Increment Usage
        if response.status_code < 400:
            logger.info(f"Request successful, incrementing usage for {model_name}")
            background_tasks.add_task(update_account_usage, account.id, model_name)
            
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers)
        )
        
    except httpx.RequestError as e:
        # Network error, maybe ban or just log?
        # For now, just return 502
        logger.error(f"Upstream request failed: {e}")
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)}")
    finally:
        await client.aclose()
