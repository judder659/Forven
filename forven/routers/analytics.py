from fastapi import APIRouter

from forven.api_domains import analytics as analytics_domain
from forven.api_domains import live_fleet as live_fleet_domain

router = APIRouter(tags=["analytics"])


@router.get("/api/stats")
def get_stats():
    return analytics_domain.get_stats()


@router.get("/api/pipeline/funnel")
def get_pipeline_funnel():
    return analytics_domain.get_pipeline_funnel()


@router.get("/api/pipeline/code-review-log")
def get_code_review_log(days: int = 7, limit: int = 50):
    return analytics_domain.get_code_review_log(days=days, limit=limit)


@router.get("/api/pipeline/funnel-report")
def get_funnel_report(days: int = 7):
    return analytics_domain.get_funnel_report(days=days)


@router.get("/api/pipeline/model-performance")
def get_model_performance():
    return analytics_domain.get_model_performance()


@router.get("/api/scanner/scans")
def list_scanner_scans_stub(limit: int = 200):
    return analytics_domain.list_scanner_scans_stub(limit=limit)


@router.get("/api/scanner/indicator-groups")
def get_scanner_indicator_groups():
    return analytics_domain.get_scanner_indicator_groups_stub()


@router.get("/api/tournaments")
def list_tournaments_stub(limit: int = 200):
    return analytics_domain.list_tournaments_stub(limit=limit)


@router.get("/api/dashboard/funnel")
def dashboard_funnel_stub():
    return analytics_domain.dashboard_funnel_stub()


# Sync `def` so the DB reads run in the threadpool, not on the request loop.
@router.get("/api/dashboard/live-fleet")
def dashboard_live_fleet():
    return live_fleet_domain.build_live_fleet()


@router.get("/api/dashboard/kpis")
def dashboard_kpis_stub():
    return analytics_domain.get_dashboard_kpis_stub()


@router.get("/api/dashboard/overview")
def dashboard_overview_stub():
    return analytics_domain.get_dashboard_overview_stub()


@router.get("/api/dashboard/activity")
def dashboard_activity_stub(limit: int = 50):
    return analytics_domain.get_dashboard_activity_stub(limit=limit)


@router.get("/api/dashboard/actions")
def dashboard_actions_stub():
    return analytics_domain.get_dashboard_actions_stub()


@router.get("/api/dashboard/leaderboard")
def dashboard_leaderboard_stub(
    sort_by: str = "sharpe_ratio",
    limit: int = 30,
    min_sharpe: float | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    tier: str | None = None,
):
    return analytics_domain.get_dashboard_leaderboard_stub(
        sort_by=sort_by,
        limit=limit,
        min_sharpe=min_sharpe,
        symbol=symbol,
        timeframe=timeframe,
        tier=tier,
    )


@router.get("/api/dashboard/coverage")
def dashboard_coverage_stub():
    return analytics_domain.get_dashboard_coverage_stub()


@router.get("/api/dashboard/tier-distribution")
def dashboard_tier_distribution_stub(scan_id: str | None = None):
    return analytics_domain.get_dashboard_tier_distribution_stub(scan_id=scan_id)


@router.get("/api/dashboard/winners")
def dashboard_winners_stub(limit: int = 10):
    return analytics_domain.get_dashboard_winners_stub(limit=limit)


@router.get("/api/dashboard/equity-curves")
def dashboard_equity_curves_stub(scan_id: str | None = None, n: int = 5):
    return analytics_domain.get_dashboard_equity_curves_stub(scan_id=scan_id, n=n)


@router.get("/api/dashboard/exceptions")
def dashboard_exceptions_stub(limit: int = 30):
    return analytics_domain.dashboard_exceptions_stub(limit=limit)


@router.get("/api/dashboard/suggestions")
def dashboard_suggestions_stub():
    return analytics_domain.dashboard_suggestions_stub()


@router.get("/api/research/feed/metrics")
def get_research_feed_metrics_stub():
    return analytics_domain.get_research_feed_metrics_stub()


@router.get("/api/strategies/performance")
def get_strategy_performance():
    return analytics_domain.get_strategy_performance()
