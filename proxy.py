import json
import random
import httpx
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from fastapi import APIRouter, Request, HTTPException, Response, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
from database import get_db, Account, AsyncSessionLocal
from config import settings

import logging

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

router = APIRouter()

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
    
    stmt = select(Account).where(
        and_(
            or_(Account.is_banned == False, Account.ban_until < now),
            Account.session_id.isnot(None),
            usage_condition
        )
    )
    
    result = await db.execute(stmt)
    accounts = result.scalars().all()
    
    if not accounts:
        return None
        
    # Random selection for load balancing
    return random.choice(accounts)

async def update_account_usage(account_id: int, model_name: str):
    """
    Increment usage counter for the account and model.
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
            
        await db.commit()

async def ban_account_temp(account_id: int, hours: int = 12):
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
