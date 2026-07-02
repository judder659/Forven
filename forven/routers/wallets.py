"""Named-wallet / Hyperliquid sub-account management API."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from forven.api_security import require_operator_access
from forven.exchange import subaccounts

router = APIRouter(tags=["wallets"], dependencies=[Depends(require_operator_access)])


class WalletRegisterRequest(BaseModel):
    label: str
    address: str


class SubaccountCreateRequest(BaseModel):
    name: str


class WalletTransferRequest(BaseModel):
    amount_usd: float = Field(gt=0)
    direction: str = "deposit"  # "deposit" (master → sub) | "withdraw" (sub → master)


class ClassTransferRequest(BaseModel):
    wallet: str | None = None  # None/"master" = master wallet; else a named-wallet label
    amount_usd: float = Field(gt=0)
    to_perp: bool = True  # True: spot → perp; False: perp → spot


@router.get("/api/wallets")
def list_wallets(light: bool = False):
    return subaccounts.list_wallets(light=light)


@router.post("/api/wallets/register")
def register_wallet(body: WalletRegisterRequest):
    try:
        return subaccounts.register_wallet(body.label, body.address)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/wallets/subaccounts")
def create_subaccount(body: SubaccountCreateRequest):
    try:
        return subaccounts.create_subaccount(body.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/wallets/{label}/transfer")
def transfer_wallet(label: str, body: WalletTransferRequest):
    direction = str(body.direction or "").strip().lower()
    if direction not in ("deposit", "withdraw"):
        raise HTTPException(status_code=400, detail="direction must be 'deposit' or 'withdraw'")
    try:
        return subaccounts.transfer(label, body.amount_usd, deposit=direction == "deposit")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/wallets/class-transfer")
def class_transfer(body: ClassTransferRequest):
    try:
        return subaccounts.class_transfer(body.wallet, body.amount_usd, to_perp=body.to_perp)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api/wallets/{label}")
def remove_wallet(label: str):
    try:
        return subaccounts.remove_wallet(label)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
