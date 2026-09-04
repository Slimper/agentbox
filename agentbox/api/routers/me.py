from fastapi import APIRouter, Depends

from agentbox.api.auth import Principal, authenticate
from agentbox.api.schemas import MeOut

router = APIRouter(prefix="/v1", tags=["me"])


@router.get("/me", response_model=MeOut)
async def me(principal: Principal = Depends(authenticate)) -> MeOut:
    return MeOut(organization_id=principal.organization_id, api_key_id=principal.api_key_id,
                 scopes=sorted(principal.scopes), environment=principal.environment)
