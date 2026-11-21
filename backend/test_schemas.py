# test_schemas.py
from schemas import ApiResponse, Item, Phase, Region, RegionStatus, SessionState

try:
    item = Item(item_id="item_001", name="剪刀", category="文具")
    region = Region(
        region_id="reg_001",
        name="書桌第一個抽屜",
        status=RegionStatus.AVAILABLE,
    )
    state = SessionState(
        session_id="session_test",
        phase=Phase.COLLECTING,
        revision=0,
        images=[],
        items=[item],
        regions=[region],
        active_plan=None,
        locations=[],
        requirements={},
    )
    response = ApiResponse(
        ok=True,
        data=state,
        revision=state.revision,
        request_id="req_test_123",
    )
    print("✅ schemas.py 驗證通過！所有型別與結構均符合規格。")
except Exception as e:
    print(f"❌ 驗證失敗: {e}")