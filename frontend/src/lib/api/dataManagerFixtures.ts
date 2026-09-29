/**
 * Realistic Data Manager fixtures for unit tests and the local dev mock.
 *
 * Built from a snapshot of the live lake taken 2026-09-28 19:22 UTC with
 * read-only GETs (/api/datasets, /api/data/coverage, /api/data/universe):
 * 97 symbols, 360 canonical candle series, the enrichment streams next to them
 * and the real 50-symbol research plan (180 planned series, 44 missing). The
 * consumers (live/paper/pipeline strategies) are illustrative.
 *
 * Every builder takes `now`; ages in the snapshot are relative to it, so the
 * dev mock can serve current-looking data. SLA states are computed with the
 * default policy of forven/dataeng/sla.py (fixtures only: the UI never does).
 */
import type {
	Bar,
	BarsResponse,
	CatalogFacets,
	CatalogResponse,
	CatalogRow,
	CollectorStatus,
	ConsumerDetail,
	ConsumerRef,
	DataJob,
	DataJobSummary,
	DataLogEntry,
	DataLogResponse,
	DataStream,
	DeleteCheck,
	DownloadEstimate,
	DownloadEstimateResponse,
	DownloadRequestItem,
	GapSpan,
	GapsResponse,
	IdentityAuditResponse,
	IdentityResolveResponse,
	ImportPreview,
	MonthCell,
	ReclaimGroup,
	RowsResponse,
	SeriesDetail,
	SeriesKey,
	SlaAssessment,
	SlaCensus,
	SlaSeriesRow,
	SlaState,
	SlaTier,
	StorageInventory,
	StreamPointsResponse,
	StreamSummary,
	SymbolCandidate,
	TrashResponse,
	UniversePlanDiff,
	VenueTarget,
	VenueTargetsResponse,
	VenuesResponse,
} from './dataManagerTypes';

export const FIXTURE_NOW = '2026-09-28T19:22:00Z';

// SYMBOL SOURCE MARKET | candles tf@first@age_min@rows | oi tf@first@age_min | funding first@age_min | basis first@age_min
const LAKE = `
1000PEPE-USDT binanceusdm perp|1h@2023-05-05@142@29810 4h@2023-05-05@442@7452 1d@2023-05-05@2602@1242|1h@2023-05-05@82 4h@2023-05-05@202|2023-05-05@202|2023-05-05@82
AAVE-USDT binanceusdm perp|1m@2020-10-16@120@3129743 5m@2020-10-16@347@625904 15m@2020-10-16@397@208632 30m@2020-10-16@202@104323 1h@2020-10-16@142@52163 4h@2020-10-16@442@13041 6h@2020-10-16@442@8694 1d@2020-10-16@2602@2173 1w@2020-10-12@21322@310|1h@2021-12-01@82 4h@2021-12-01@202|2020-10-16@202|2026-06-11@82
ADA-BTC binance spot|1h@2025-09-25@442@8837 1d@2017-11-30@2602@3224|||
ADA-USDT binanceusdm perp|1m@2020-01-31@119@3502643 5m@2020-01-31@252@700503 15m@2020-01-31@397@233492 30m@2020-01-31@202@116753 1h@2020-01-31@142@58378 4h@2020-01-31@442@14594 1d@2020-01-31@2602@2432 1w@2020-01-27@21322@347|15m@2026-06-09@5932 1h@2021-12-01@82 4h@2021-12-01@202 1d@2026-08-10@15562|2020-01-19@202|2020-01-31@82
ALLO-USDT binanceusdm perp|1h@2025-11-11@442@7703 4h@2025-11-11@442@1927 1d@2025-11-11@2602@321|1h@2025-11-11@28582 4h@2025-11-11@28762|2025-11-11@27802|2025-11-11@102922
ALT-BTC binance spot|1h@2025-07-01@267442@6455|||
ALT-USDT binanceusdm perp|15m@2024-01-25@367@93790 1h@2024-01-25@142@23452 4h@2024-01-25@442@5863|15m@2026-07-15@100837 1h@2024-01-25@82 4h@2024-01-25@202|2024-01-25@202|2026-06-11@82
APT-USDT binanceusdm perp|1m@2022-10-19@119@2074524 5m@2022-10-19@157@414898 15m@2022-10-19@262@138293 30m@2022-10-19@292@69146 1h@2022-10-19@142@34576 4h@2022-10-19@442@8644 1d@2022-10-19@2602@1440 1w@2022-10-17@21322@205|15m@2026-06-10@130987 1h@2022-10-19@82 4h@2022-10-19@202|2022-10-18@202|2026-06-11@82
ARB-USDT binanceusdm perp|5m@2023-03-23@157@370102 15m@2026-09-08@367@1947 1h@2023-03-23@142@30843 4h@2023-03-23@442@7711|1h@2023-03-23@82 4h@2023-03-23@202|2023-03-23@202|2026-06-11@82
ARX-USDT binanceusdm perp|1h@2026-06-23@442@2337 4h@2026-06-23@442@585 1d@2026-06-23@2602@97|1h@2026-06-23@28582 4h@2026-06-23@28762|2026-06-23@27802|2026-06-23@123622
ATOM-USDT binanceusdm perp|5m@2020-02-07@252@698437 15m@2020-02-07@367@232806 1h@2020-02-07@142@58206 4h@2020-02-07@442@14551|1h@2021-12-01@82 4h@2021-12-01@202|2020-02-06@202|2026-06-16@82
AVAX-USDT binanceusdm perp|5m@2020-09-23@252@632547 15m@2020-09-23@262@210849 1h@2020-09-23@142@52715 4h@2020-09-23@442@13179 1d@2020-09-23@2602@2196|5m@2026-07-02@120682 15m@2026-06-26@128692 1h@2021-12-01@82 4h@2021-12-01@202 1d@2026-08-10@1162|2020-09-22@202|2020-09-23@82
BCH-USDT binanceusdm perp|1h@2020-01-01@142@59106 4h@2020-01-01@442@14776 1d@2020-01-01@2602@2462|1h@2021-12-01@82 4h@2021-12-01@202|2020-01-01@202|2020-01-01@82
BIRB-USDT binanceusdm perp|1h@2026-01-29@262@5819 4h@2026-01-29@442@1455 1d@2026-01-29@2602@242|1h@2026-01-29@28582 4h@2026-01-29@28762|2026-01-29@27802|2026-01-29@123622
BNB-BTC binance spot|4h@2017-07-14@442@20165 1d@2017-07-14@2602@3363|||
BNB-USDT binanceusdm perp|5m@2020-02-10@252@697623 15m@2020-02-10@262@232541 1h@2020-02-10@142@58138 4h@2020-02-10@442@14534 1d@2020-02-10@2602@2422|15m@2026-06-09@145132 1h@2021-12-01@82 4h@2026-05-02@202 1d@2026-08-10@19882|2020-02-10@202|2020-02-10@82
BREV-USDT binanceusdm perp|1h@2025-12-30@262@6534 4h@2025-12-30@442@1634 1d@2025-12-30@2602@272|1h@2025-12-30@28582 4h@2025-12-30@28762|2025-12-30@27802|2025-12-30@126502
BTC-USD binance spot|1h@2023-09-22@262@26453 4h@2024-03-31@442@1791 1d@2019-12-10@2602@2484|||
BTC-USDC binanceusdm perp|1m@2024-01-04@408@1437124 5m@2024-01-04@157@287476 15m@2024-01-04@397@95810 1h@2024-01-04@262@23956 4h@2024-01-04@442@5989 1d@2024-01-04@2602@998|30m@2024-02-01@125002 1h@2024-01-04@28582 4h@2024-02-01@125002|2024-01-02@28042|2024-01-04@123622
BTC-USDT binanceusdm perp|1m@2020-01-01@407@3546036 5m@2020-01-01@127@709264 15m@2020-01-01@262@236413 30m@2020-01-01@142@118211 1h@2019-09-08@142@61849 4h@2019-09-08@442@15462 8h@2019-09-08@682@7731 1d@2019-09-08@2602@2577 1w@2020-01-06@21322@350|5m@2025-07-02@132022 15m@2026-05-30@4477 30m@2020-09-01@80842 1h@2020-09-01@82 4h@2020-09-01@202 1d@2025-07-02@1162|2020-01-01@202|2020-01-01@82
BTCUSD binance spot|1h@2025-12-04@262@7160|||
BTW-USDT binanceusdm perp|1h@2026-06-04@262@2786 4h@2026-06-04@442@697 1d@2026-06-04@2602@116|1h@2026-07-01@123562 4h@2026-07-01@123562|2026-06-04@27802|2026-06-05@123622
CAP-USDT binanceusdm perp|1h@2026-06-27@262@2237 4h@2026-06-27@442@560 1d@2026-06-27@2602@93|1h@2026-07-01@123562 4h@2026-07-01@123562|2026-06-27@27802|2026-06-30@123622
CL-USDT binanceusdm perp|1h@2026-04-01@262@4327 4h@2026-04-01@442@1082 1d@2026-04-01@2602@180|1h@2026-04-01@77182 4h@2026-04-01@77242|2026-03-31@27802|2026-03-31@77182
COPPER-USDT binanceusdm perp|1d@2026-03-06@2602@206||2026-04-24@27802|
CRCL-USDT binanceusdm perp|1h@2026-02-09@262@5546 4h@2026-02-09@442@1387 1d@2026-02-09@2602@231|1h@2026-03-01@125002 4h@2026-03-01@125002|2026-02-06@28042|2026-02-06@126502
DOGE-USDT binanceusdm perp|5m@2020-07-10@127@654148 15m@2020-07-10@112@218051 1h@2020-07-10@142@54513 4h@2020-07-10@442@13628 1d@2020-07-10@2602@2271|15m@2026-05-27@38137 1h@2021-12-01@82 4h@2021-12-01@202 1d@2026-08-10@19882|2020-07-10@202|2020-07-10@82
DOT-USDT binanceusdm perp|5m@2020-08-22@127@641788 1h@2025-09-19@202@8993 4h@2020-08-22@442@13371|15m@2026-09-21@3262 1h@2021-12-01@82 4h@2021-12-01@202|2020-08-20@202|2026-08-29@82
DRAM-USDT binanceusdm perp|5m@2026-05-18@347@38300 15m@2026-05-18@397@12764 1h@2026-05-18@202@3195 4h@2026-05-18@442@799 1d@2026-05-18@2602@133|1h@2026-06-01@125002 4h@2026-06-01@125002|2026-05-18@28042|2026-05-18@123622
ENA-USDT binanceusdm perp|1h@2024-04-02@142@21822 4h@2024-04-02@442@5455 1d@2024-04-02@2602@909|1h@2024-05-01@82 4h@2024-05-01@202|2024-04-02@202|2024-04-02@82
EPIC-USDT binanceusdm perp|1h@2025-03-13@442@13541 4h@2025-03-13@442@3386 1d@2025-03-13@2602@564|1h@2025-07-06@123562 4h@2025-07-06@123562|2025-03-13@27802|2025-03-13@123622
ETH-BTC binance spot|1h@2023-04-26@442@30025 4h@2017-07-14@442@20165 1d@2017-07-14@2602@3363|15m@2023-05-01@125017 1h@2023-05-01@125062 4h@2023-05-01@125242|2023-04-24@129802|
ETH-USD binance spot|1h@2025-10-06@442@5621|||
ETH-USDC binanceusdm perp|1m@2024-01-04@375@1437152 5m@2024-01-04@347@287437 15m@2024-01-04@397@95810 1h@2024-01-04@442@23953 4h@2024-01-04@442@5989 1d@2024-01-04@2602@998|15m@2024-02-01@125002 1h@2024-02-01@125002 4h@2024-02-01@125002|2024-01-02@28042|2024-01-04@123622
ETH-USDT binanceusdm perp|1m@2020-01-01@216@3546227 5m@2019-12-19@347@712763 15m@2020-01-01@97@236424 30m@2020-01-01@412@118202 1h@2019-11-27@142@59939 4h@2019-11-27@442@14985 8h@2020-01-01@682@7388 1d@2020-01-01@2602@2462 1w@2020-01-06@21322@350|5m@2026-06-18@125907 15m@2021-12-01@52 1h@2021-12-01@82 4h@2021-12-01@202 1d@2026-05-05@15562|2020-01-01@202|2020-01-01@82
EWY-USDT binanceusdm perp|5m@2026-03-16@347@56450 15m@2026-03-16@397@18814 1h@2026-03-16@442@4704 4h@2026-03-16@442@1177 1d@2026-03-16@2602@196|1h@2026-04-01@125002 4h@2026-04-01@125002|2026-03-13@28042|2026-03-13@123622
FIL-USDT binanceusdm perp|5m@2020-10-16@347@625916|1h@2021-12-01@125002 4h@2021-12-01@125002|2020-10-16@28042|
GLW-USDT binanceusdm perp|1h@2026-06-11@382@2621 4h@2026-06-11@442@656 1d@2026-06-11@2602@109|1h@2026-07-01@125002 4h@2026-07-01@125002|2026-06-11@28042|2026-06-30@127942
HMSTR-USDT binanceusdm perp|5m@2024-09-26@252@210849 15m@2024-09-26@397@70274 1h@2024-09-26@142@17574 4h@2024-09-26@442@4393 1d@2024-09-26@2602@732|1h@2024-10-01@82 4h@2024-10-01@202|2024-09-26@202|2024-09-26@82
HOT-USDT binanceusdm perp|1h@2021-03-30@142@48203 4h@2021-03-30@442@12051 1d@2021-03-30@2602@2008|1h@2025-07-06@82 4h@2025-07-06@202|2021-03-30@202|2021-03-30@82
HYPE-USDT binanceusdm perp|1m@2025-05-30@375@699998 5m@2025-05-30@252@140025 15m@2025-05-30@397@46666 1h@2025-05-30@382@11668 4h@2025-05-30@442@2918 1d@2025-05-30@2602@486|5m@2026-07-13@98712 1h@2025-06-01@17902 4h@2025-06-01@29962 1d@2026-08-09@29962|2025-05-30@18202|2025-05-30@17902
ICP-USDT binanceusdm perp|5m@2021-05-11@222@558843|1h@2022-10-01@125002 4h@2022-10-01@125002|2022-09-01@28042|
INJ-USDT binanceusdm perp|5m@2022-08-17@222@433020|1h@2022-09-01@125002 4h@2022-09-01@125002|2022-08-16@28042|
INTC-USDT binanceusdm perp|1h@2026-02-02@382@5712 4h@2026-02-02@442@1429 1d@2026-02-02@2602@238|1h@2026-03-01@125002 4h@2026-03-01@125002|2026-02-02@28042|2026-02-02@126502
KORU-USDT binanceusdm perp|5m@2026-06-22@222@28246 15m@2026-06-22@202@9418 1h@2026-06-22@322@2354 4h@2026-06-22@442@589 1d@2026-06-22@2602@98|1h@2026-07-01@125002 4h@2026-07-01@125002|2026-06-22@28042|2026-06-23@123622
LAB-USDT binanceusdm perp|1m@2025-10-17@375@498158 5m@2025-10-17@222@99663 15m@2025-10-17@202@33223 1h@2025-10-17@322@8305 4h@2025-10-17@442@2077 1d@2025-10-17@2602@346|1h@2025-11-01@125002 4h@2025-11-01@125002|2025-10-17@27802|2025-10-17@123622
LINK-USDT binanceusdm perp|5m@2020-01-17@222@704541 15m@2020-01-17@82@234857 1h@2020-01-17@142@58714 4h@2020-01-17@442@14678 1d@2020-01-17@2602@2446|1h@2021-12-01@82 4h@2021-12-01@202|2020-01-17@202|2020-01-17@82
LIT-USDT binanceusdm perp|1h@2021-02-18@142@49026 4h@2021-02-18@442@12257 1d@2021-02-18@2602@2043|1h@2021-12-01@82 4h@2021-12-01@202|2021-02-18@202|2021-02-18@82
M-USDT binanceusdm perp|1h@2025-07-07@322@10758 4h@2025-07-07@442@2690 1d@2025-07-07@2602@448|1h@2025-08-01@125002 4h@2025-08-01@125002|2025-07-07@27802|2025-07-07@126502
MAGMA-USDT binanceusdm perp|1h@2025-12-31@322@6506 4h@2025-12-31@442@1627 1d@2025-12-31@2602@271|1h@2026-01-01@126442 4h@2026-01-01@126442|2025-12-31@27802|2025-12-31@126502
MATIC-USDT binance spot|4h@2019-04-26@1078282@11778|1h@2021-12-01@885202 4h@2021-12-01@885322|2020-10-22@129802|
MRVL-USDT binanceusdm perp|1h@2026-05-15@322@3265 4h@2026-05-15@442@817 1d@2026-05-15@2602@136|1h@2026-06-01@125002 4h@2026-06-01@125002|2026-05-14@28042|2026-05-16@126502
MSTR-USDT binanceusdm perp|5m@2026-02-09@222@66543 15m@2026-02-09@397@22170 1h@2026-02-09@322@5545 4h@2026-02-09@442@1387 1d@2026-02-09@2602@231|1h@2026-03-01@125002 4h@2026-03-01@125002|2026-02-06@28042|2026-02-06@123622
MU-USDT binanceusdm perp|1m@2026-04-07@216@250698 5m@2026-04-07@222@50140 15m@2026-04-07@397@16703 1h@2026-04-07@322@4178 4h@2026-04-07@442@1045 1d@2026-04-07@2602@174|1h@2026-05-01@125002 4h@2026-05-01@125002|2026-04-07@28042|2026-04-07@123622
MULTI-USDT binance spot|1h@2022-04-06@1370482@16438 4h@2023-09-06@1370602@1000 1d@2022-04-06@1370602@686|||
NEAR-USDT binanceusdm perp|5m@2020-10-15@222@626205 15m@2020-10-15@397@208724 1h@2020-10-15@202@52185 4h@2020-10-15@442@13046 1d@2020-10-15@2602@2174|5m@2026-05-31@168942 1h@2021-12-01@82 4h@2021-12-01@202|2020-10-14@202|2020-10-15@82
O-USDT binanceusdm perp|1h@2026-06-24@322@2308 4h@2026-06-24@442@578 1d@2026-06-24@2602@96|1h@2026-07-01@123562 4h@2026-07-01@123562|2026-06-24@27802|2026-06-25@123622
OP-USDT binanceusdm perp|5m@2022-06-01@192@455067 1h@2022-06-01@202@37923 4h@2025-09-26@442@2203|1h@2022-06-01@82 4h@2022-06-01@202|2022-06-01@202|2026-06-16@82
PAXG-USDT binanceusdm perp|1h@2025-07-14@322@10581|1h@2026-06-23@99502 4h@2026-06-14@99562|2026-04-22@27802|2026-06-23@99562
PEPE-USDT binance spot|5m@2026-02-22@192@62903 15m@2026-03-17@397@18737 1h@2026-01-27@322@5867|||
PERP-USDT binanceusdm perp|1h@2025-07-13@262@10612||2025-08-30@448522|2025-12-02@402442
POL-USDT binanceusdm perp|5m@2024-09-13@192@214608 1h@2024-09-13@142@17886 4h@2026-03-31@442@1090|1h@2024-10-01@82 4h@2024-10-01@202|2024-09-13@202|2026-08-23@82
QQQ-USDT binanceusdm perp|1h@2026-04-06@262@4203 4h@2026-04-06@442@1051 1d@2026-04-06@2602@175|1h@2026-05-01@125002 4h@2026-05-01@125002|2026-04-03@28042|2026-04-03@126502
RE-USDT binanceusdm perp|1h@2026-06-18@262@2450 4h@2026-06-18@442@613 1d@2026-06-18@2602@102|1h@2026-07-01@125002 4h@2026-07-01@125002|2026-06-18@27802|2026-06-23@123622
RETRY binance spot|1h@2026-06-21@262@2384|||
RPL-USDT binanceusdm perp|1h@2024-09-09@142@17982 4h@2024-09-09@442@4495 1d@2024-09-09@2602@749|1h@2025-07-06@82 4h@2025-07-06@202|2024-09-09@202|2024-09-09@82
RUNE-USDT binanceusdm perp|5m@2020-09-04@192@638029|1h@2021-12-01@82 4h@2021-12-01@202|2020-09-04@202|2026-09-07@82
SAMSUNG-USDT binanceusdm perp|1h@2026-06-02@262@2845 4h@2026-06-02@442@712 1d@2026-06-02@2602@118|1h@2026-07-01@123562 4h@2026-07-01@123562|2026-06-02@27802|2026-06-04@126502
SEI-USDT binanceusdm perp|5m@2023-08-17@222@327903 1h@2023-08-17@142@27328 4h@2026-01-21@442@1501|1h@2023-09-01@82 4h@2023-09-01@202|2023-08-16@202|2026-06-16@82
SKHYNIX-USDT binanceusdm perp|1m@2026-06-02@216@170687 5m@2026-06-02@222@34137 15m@2026-06-02@262@11377 1h@2026-06-02@262@2845 4h@2026-06-02@442@712 1d@2026-06-02@2602@118|1h@2026-07-01@123562 4h@2026-07-01@123562|2026-06-02@27802|2026-06-02@123622
SLX-USDT binanceusdm perp|1h@2026-06-01@262@2865 4h@2026-06-01@442@717 1d@2026-06-01@2602@119|1h@2026-06-01@123562 4h@2026-06-01@123562|2026-06-01@27802|2026-06-02@123622
SNDK-USDT binanceusdm perp|1m@2026-04-07@216@250687 5m@2026-04-07@157@50150 15m@2026-04-07@262@16711 1h@2026-04-07@262@4179 4h@2026-04-07@442@1045 1d@2026-04-07@2602@174|1h@2026-05-01@123562 4h@2026-05-01@123562|2026-04-07@28042|2026-04-07@123622
SOL-BTC binance spot|1h@2025-04-19@262@12661 4h@2020-04-10@442@14175 1d@2020-04-10@2602@2362|||
SOL-USDC binanceusdm perp|5m@2024-01-04@157@287473 15m@2024-01-04@262@95818 1h@2024-01-04@262@23956 4h@2024-01-04@442@5989 1d@2024-01-04@2602@998|15m@2025-07-06@123562 1h@2024-02-01@123562 4h@2024-02-01@123562|2024-01-02@28042|2024-01-04@123622
SOL-USDT binanceusdm perp|1m@2020-09-14@120@3175823 5m@2020-09-14@157@635158 15m@2020-09-14@52@211727 1h@2020-09-14@142@52931 4h@2020-09-14@442@13233 8h@2020-09-14@682@6617 1d@2020-09-14@2602@2205|5m@2026-06-24@135772 15m@2025-07-06@52 1h@2021-12-01@82 4h@2021-12-01@202 1d@2026-05-05@81802|2020-09-13@202|2020-09-14@82
SOXL-USDT binanceusdm perp|1m@2026-05-15@442@195721 5m@2026-05-15@127@39208 15m@2026-05-15@262@13061 1h@2026-05-15@262@3266 4h@2026-05-15@442@817 1d@2026-05-15@2602@136|1h@2026-06-01@123562 4h@2026-06-01@123562|2026-05-14@28042|2026-05-17@123622
SPCX-USDT binanceusdm perp|5m@2026-05-21@127@37591 15m@2026-05-21@172@12528 1h@2026-05-21@262@3132 4h@2026-05-21@682@782 1d@2026-05-21@2602@130|1h@2026-06-01@123562 4h@2026-06-01@123562|2026-05-21@28042|2026-05-22@123622
STX-USDT binanceusdm perp|5m@2023-02-21@127@378754|1h@2023-03-01@123562 4h@2023-03-01@123562|2023-02-21@28042|
SUI-USDT binanceusdm perp|5m@2023-05-03@447@358224 1h@2023-05-03@142@29858 4h@2023-05-03@442@7464 1d@2023-05-03@2602@1244|1h@2023-06-01@82 4h@2023-06-01@202|2023-05-03@202|2023-05-03@82
SYN-USDT binanceusdm perp|1h@2024-08-16@142@18558 4h@2024-08-16@442@4639 1d@2024-08-16@2602@773|1h@2024-09-01@82 4h@2024-09-01@202|2024-08-16@202|2024-08-16@82
TAIKO-USDT binanceusdm perp|5m@2025-06-11@447@136506 15m@2025-06-11@172@45521 1h@2025-06-11@202@11381 4h@2025-06-11@682@2844 1d@2025-06-11@2602@474|1h@2025-07-01@123562 4h@2025-07-01@123562|2025-06-11@27802|2025-06-11@126502
TIA-USDT binanceusdm perp|5m@2023-10-31@417@306078 1h@2023-10-31@142@25512 4h@2026-01-21@442@1501|1h@2023-11-01@82 4h@2023-11-01@202|2023-10-31@202|2026-06-16@82
TLM-USDT binanceusdm perp|1m@2021-07-16@441@2694002 5m@2021-07-16@412@538807 15m@2021-07-16@172@179619 1h@2021-07-16@142@44907 4h@2021-07-16@442@11227 1d@2021-07-16@2602@1871|1h@2021-12-01@82 4h@2021-12-01@202|2023-03-01@202|2021-07-16@82
TON-USDT binanceusdm perp|5m@2024-03-01@412@271009|1h@2024-03-01@124822 4h@2024-03-01@125002|2024-03-01@140362|
TRX-USDT binanceusdm perp|5m@2020-01-15@412@705078|1h@2021-12-01@123562 4h@2021-12-01@123562|2020-01-15@28042|
TSLA-USDT binanceusdm perp|1h@2026-01-28@202@5835 4h@2026-01-28@682@1458 1d@2026-01-28@2602@243|1h@2026-02-01@123562 4h@2026-02-01@123562|2026-01-27@28042|2026-01-27@126502
UNI-USDT binanceusdm perp|1h@2020-09-18@82@52836 4h@2020-09-18@442@13209 1d@2020-09-18@2602@2201|1h@2021-12-01@82 4h@2021-12-01@202|2020-09-17@202|2020-09-18@82
USDT-TRY binance spot|1h@2026-06-01@502@2860|||
USDT-USD binance spot|1h@2025-11-18@502@7533 4h@2025-11-18@682@1884|||
VANRY-USDT binanceusdm perp|1m@2024-03-13@441@1337432 5m@2024-03-13@412@267493 15m@2024-03-13@172@89181 1h@2024-03-13@502@22291 4h@2024-03-13@682@5573 1d@2024-03-13@2602@929|1h@2025-07-06@75502 4h@2025-07-06@75562|2024-03-13@75562|2024-03-13@75262
VELVET-USDT binanceusdm perp|1h@2025-07-15@502@10563 4h@2025-07-15@682@2641 1d@2025-07-15@2602@440|1h@2025-08-01@123562 4h@2025-08-01@123562|2025-07-15@27802|2025-07-15@123622
WLD-USDT binanceusdm perp|1h@2023-07-24@142@27894 4h@2023-07-24@442@6973 1d@2023-07-24@2602@1162|1h@2025-07-02@82 4h@2025-07-02@202|2023-07-24@202|2023-07-24@82
XAG-USDT binanceusdm perp|1m@2026-01-07@343@380380 5m@2026-01-07@412@76063 15m@2026-01-07@172@25371 1h@2026-01-07@502@6338 4h@2026-01-07@682@1585 1d@2026-01-07@2602@264|1h@2026-02-01@123562 4h@2026-02-01@123562|2026-01-07@27802|2026-01-07@123622
XAU-USDT binanceusdm perp|1m@2025-12-11@343@419375 5m@2025-12-11@157@83913 15m@2025-12-11@142@27973 1h@2025-12-11@502@6988 4h@2025-12-11@682@1747 1d@2025-12-11@2602@291|1h@2026-01-01@123562 4h@2026-01-01@123562|2025-12-11@27802|2025-12-11@123622
XLM-USDT binanceusdm perp|5m@2020-01-20@127@703696 1h@2020-01-20@202@58641 4h@2020-01-20@442@14660 1d@2020-01-20@2602@2443|5m@2026-06-03@165297 1h@2025-07-02@82 4h@2025-07-02@202|2020-01-19@202|2020-01-20@82
XRP-USDT binanceusdm perp|1m@2020-01-06@343@3538399 5m@2020-01-06@287@707692 15m@2020-01-06@397@235891 1h@2020-01-06@142@58978 4h@2020-01-06@442@14744 1d@2020-01-06@2602@2457|5m@2026-09-26@287 15m@2026-06-10@145132 1h@2025-07-02@82 4h@2025-07-02@202 1d@2026-08-10@19882|2020-01-06@202|2020-01-06@82
ZEC-USDT binanceusdm perp|1m@2020-02-05@120@3495442 5m@2020-02-05@447@699024 15m@2020-02-05@397@233012 1h@2020-02-05@142@58258 4h@2020-02-05@442@14564 1d@2020-02-05@2602@2427|1h@2025-07-02@82 4h@2025-07-02@202|2020-02-04@202|2020-02-05@82
`;

// ---------------------------------------------------------------- helpers

const MINUTE = 60_000;
const DAY = 86_400_000;
const TF_SECONDS: Record<string, number> = {
	'1m': 60, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '2h': 7200, '4h': 14400,
	'6h': 21600, '8h': 28800, '12h': 43200, '1d': 86400, '1w': 604800,
};
export const tfSeconds = (tf: string) => TF_SECONDS[tf] ?? 3600;

function hash(text: string): number {
	let h = 2166136261;
	for (let i = 0; i < text.length; i++) {
		h ^= text.charCodeAt(i);
		h = Math.imul(h, 16777619);
	}
	return h >>> 0;
}

/** Deterministic 0..1 from a string. */
const unit = (text: string) => (hash(text) % 100_000) / 100_000;

const iso = (ms: number) => new Date(ms).toISOString().replace('.000Z', 'Z');
const ms = (value: string) => Date.parse(value);
const toMs = (now: string | number | Date) => (now instanceof Date ? now.getTime() : typeof now === 'number' ? now : ms(now));
const display = (symbol: string) => (symbol.includes('-') ? symbol.replace('-', '/') : symbol);

/** Snap to the open of the bar containing `t` (weeks open on Monday). */
function barOpen(t: number, tf: string): number {
	const step = tfSeconds(tf) * 1000;
	if (tf === '1w') return Math.floor((t - 4 * DAY) / step) * step + 4 * DAY;
	return Math.floor(t / step) * step;
}

// ---------------------------------------------------------------- the lake

const POLICY: SlaCensus['policy'] = {
	live: { missed_bars: 1, floor_minutes: 20 },
	paper: { missed_bars: 2, floor_minutes: 45 },
	pipeline: { missed_bars: 3, floor_minutes: 120 },
	universe: { missed_bars: 6, floor_minutes: 360 },
	idle: { missed_bars: 24, floor_minutes: 1440 },
};
const TIER_WEIGHT: Record<SlaTier, number> = { live: 100, paper: 50, pipeline: 20, universe: 5, idle: 1 };
const TIER_ORDER: SlaTier[] = ['live', 'paper', 'pipeline', 'universe', 'idle'];
const BREACH_MULTIPLIER = 3;
const BYTES_PER_ROW: Record<DataStream, number> = {
	ohlcv: 23, funding: 20, oi: 8, basis: 18, iv: 16, ls_ratio: 5, taker: 6, liquidations: 22,
};

/** The real research plan (liquidity rank order). */
const PLAN: Array<[string, string[]]> = [
	'BTC-USDT', 'ETH-USDT', 'BTC-USDC', 'XAU-USDT', 'QNT-USDT', 'ETH-USDC', 'SOL-USDT', 'SPCX-USDT', 'ZEC-USDT', 'SOXL-USDT',
].map((s): [string, string[]] => [s, ['1h', '4h', '1d', '15m', '5m', '1m']]).concat(
	[
		'XRP-USDT', 'CL-USDT', 'SNDK-USDT', 'XAG-USDT', 'NEAR-USDT', 'HYPE-USDT', 'HBAR-USDT', 'SKHYNIX-USDT', 'SUI-USDT', 'DOGE-USDT',
		'BZ-USDT', 'PUMP-USDT', 'ONDO-USDT', 'LINK-USDT', 'UNI-USDT', 'ENA-USDT', 'MU-USDT', 'WLD-USDT', 'KORU-USDT', 'HFT-USDT',
		'BNB-USDT', 'BTW-USDT', 'CRCL-USDT', 'SOL-USDC', 'SOXS-USDT', 'TAO-USDT', 'ADA-USDT', 'SKHY-USDT', 'INTC-USDT', '1000PEPE-USDT',
		'SNXX-USDT', 'NVDA-USDT', 'SAMSUNG-USDT', 'AVAX-USDT', 'MSTR-USDT', 'XLM-USDT', 'BCH-USDT', 'ARB-USDT', 'MARSCOIN-USDT', 'LTC-USDT',
	].map((s): [string, string[]] => [s, ['1h', '4h', '1d']]),
);

const DELISTED: Record<string, string> = {
	'MATIC-USDT': '2024-09-10', 'MULTI-USDT': '2023-07-14', 'PEPE-USDT': '2026-03-18',
	'PERP-USDT': '2025-12-02', 'TON-USDT': '2026-07-29', 'VANRY-USDT': '2026-09-02',
};
const STRUCK_OUT = new Set(['ohlcv:canonical:ALT-BTC:1h']);

const ASSET_CLASS: Record<string, string> = {
	XAU: 'tradfi', XAG: 'tradfi', CL: 'tradfi', COPPER: 'tradfi', BZ: 'tradfi',
	TSLA: 'stock', MSTR: 'stock', MU: 'stock', INTC: 'stock', SAMSUNG: 'stock', SKHYNIX: 'stock', SKHY: 'stock', SNDK: 'stock',
	MRVL: 'stock', CRCL: 'stock', GLW: 'stock', NVDA: 'stock', SPCX: 'stock',
	SOXL: 'etf', SOXS: 'etf', QQQ: 'etf', EWY: 'etf', KORU: 'etf', DRAM: 'etf', SLX: 'etf', SNXX: 'etf',
};
function assetClass(symbol: string): string {
	if (symbol === 'USDT-TRY' || symbol === 'USDT-USD') return 'forex';
	return ASSET_CLASS[symbol.split('-')[0]] ?? 'crypto';
}

/** Illustrative consumers. Series ids: `${stream}:${venue}:${symbol}:${timeframe}`. */
const CONSUMERS: Array<{ ref: ConsumerRef; tier: SlaTier; series: string[] }> = [
	{ ref: { kind: 'strategy', id: 'S01566', name: 'BTC trend follower', stage: 'live_graduated' }, tier: 'live',
		series: ['ohlcv:canonical:BTC-USDT:4h', 'ohlcv:hyperliquid:perp:BTC-USDT:4h'] },
	{ ref: { kind: 'strategy', id: 'S06151', name: 'BTC volatility squeeze', stage: 'live_graduated' }, tier: 'live',
		series: ['ohlcv:canonical:BTC-USDT:1h', 'ohlcv:hyperliquid:perp:BTC-USDT:1h'] },
	{ ref: { kind: 'bot', id: 'B007', name: 'BTC grid bot', status: 'running' }, tier: 'live', series: ['ohlcv:canonical:BTC-USDT:1h'] },
	{ ref: { kind: 'strategy', id: 'S06325', name: 'ETH funding reversion', stage: 'live_graduated' }, tier: 'live',
		series: ['ohlcv:canonical:ETH-USDT:4h', 'ohlcv:hyperliquid:perp:ETH-USDT:4h', 'funding:canonical:ETH-USDT:8h'] },
	{ ref: { kind: 'strategy', id: 'S03402', name: 'SOL breakout', stage: 'live_graduated' }, tier: 'live',
		series: ['ohlcv:canonical:SOL-USDT:1h', 'ohlcv:hyperliquid:perp:SOL-USDT:1h'] },
	{ ref: { kind: 'strategy', id: 'S06153', name: 'AVAX momentum', stage: 'live_graduated' }, tier: 'live',
		series: ['ohlcv:canonical:AVAX-USDT:4h', 'ohlcv:hyperliquid:perp:AVAX-USDT:4h'] },
	{ ref: { kind: 'strategy', id: 'S05665', name: 'LINK carry', stage: 'live_graduated' }, tier: 'live',
		series: ['ohlcv:canonical:LINK-USDT:1d', 'ohlcv:hyperliquid:perp:LINK-USDT:1d'] },
	{ ref: { kind: 'strategy', id: 'S10869', name: 'ETH volume thrust + IV confirm', stage: 'paper' }, tier: 'paper',
		series: ['ohlcv:canonical:ETH-USDT:1h', 'iv:canonical:ETH-USDT:1h', 'oi:canonical:ETH-USDT:1h'] },
	{ ref: { kind: 'strategy', id: 'S04928', name: 'SOL mean reversion 15m', stage: 'paper' }, tier: 'paper',
		series: ['ohlcv:canonical:SOL-USDT:15m', 'funding:canonical:SOL-USDT:8h'] },
	{ ref: { kind: 'strategy', id: 'S10912', name: 'AVAX RSI divergence', stage: 'gauntlet' }, tier: 'pipeline',
		series: ['ohlcv:canonical:AVAX-USDT:1h', 'ohlcv:canonical:AVAX-USDT:30m'] },
	{ ref: { kind: 'workflow', id: 'wf-2291', name: 'Gauntlet · S10912', status: 'running' }, tier: 'pipeline',
		series: ['ohlcv:canonical:AVAX-USDT:1h', 'ohlcv:canonical:AVAX-USDT:30m'] },
	{ ref: { kind: 'strategy', id: 'S10915', name: 'XAU session breakout', stage: 'quick_screen' }, tier: 'pipeline',
		series: ['ohlcv:canonical:XAU-USDT:1h'] },
];

interface Spec {
	symbol: string;
	stream: DataStream;
	timeframe: string;
	venue: string;
	source: string;
	market: string;
	first: number | null;
	/** Minutes from the last bar's open to `now`; null = no file. */
	age: number | null;
	rows?: number;
}

const seriesId = (s: { stream: string; venue: string; symbol: string; timeframe: string }) => `${s.stream}:${s.venue}:${s.symbol}:${s.timeframe}`;

let specCache: Spec[] | null = null;
function lakeSpecs(): Spec[] {
	if (specCache) return specCache;
	const specs: Spec[] = [];
	const day = (d: string) => ms(`${d}T00:00:00Z`);
	for (const line of LAKE.trim().split('\n')) {
		const [head, candles, oi, funding, basis] = line.split('|');
		const [symbol, source, market] = head.split(' ');
		const streamSource = source === 'binance' ? 'binance' : 'binanceusdm';
		for (const item of candles.split(' ').filter(Boolean)) {
			const [tf, first, age, rows] = item.split('@');
			specs.push({ symbol, stream: 'ohlcv', timeframe: tf, venue: 'canonical', source, market, first: day(first), age: Number(age), rows: Number(rows) });
		}
		const oiItems = oi.split(' ').filter(Boolean).map((item) => item.split('@'));
		for (const [tf, first, age] of oiItems) {
			specs.push({ symbol, stream: 'oi', timeframe: tf, venue: 'canonical', source: streamSource, market: 'perp', first: day(first), age: Number(age) });
		}
		// Long/short ratio and taker flow come from the same futures-data collector as OI.
		const extra = new Set(oiItems.map(([tf]) => tf));
		for (const tf of ['5m', '15m', '1d']) if (oiItems.length) extra.add(tf);
		for (const tf of extra) {
			const twin = oiItems.find(([t]) => t === tf) ?? oiItems.find(([t]) => t === '1h') ?? oiItems[0];
			for (const stream of ['ls_ratio', 'taker'] as const) {
				specs.push({ symbol, stream, timeframe: tf, venue: 'canonical', source: streamSource, market: 'perp', first: day(twin[1]), age: Number(twin[2]) });
			}
		}
		if (funding) {
			const [first, age] = funding.split('@');
			specs.push({ symbol, stream: 'funding', timeframe: '8h', venue: 'canonical', source: streamSource, market: 'perp', first: day(first), age: Number(age) });
			// Liquidation capture (OKX WebSocket) started 2026-07-06.
			const live = Number(age) < 600;
			specs.push({ symbol, stream: 'liquidations', timeframe: '1h', venue: 'canonical', source: 'okx', market: 'perp',
				first: Math.max(day(first), day('2026-07-06')), age: live ? 22 : Number(age) });
		}
		if (basis) {
			const [first, age] = basis.split('@');
			specs.push({ symbol, stream: 'basis', timeframe: '1h', venue: 'canonical', source: streamSource, market: 'perp', first: day(first), age: Number(age) });
		}
	}
	for (const symbol of ['BTC-USDT', 'ETH-USDT']) {
		specs.push({ symbol, stream: 'iv', timeframe: '1h', venue: 'canonical', source: 'deribit', market: 'unknown', first: day('2021-03-24'), age: 22 });
	}
	// Execution-venue candles for the live fleet, plus one venue download and one CSV import.
	for (const [symbol, tf, first] of [
		['BTC-USDT', '1h', '2023-05-12'], ['BTC-USDT', '4h', '2023-05-12'], ['ETH-USDT', '4h', '2023-05-12'],
		['SOL-USDT', '1h', '2023-05-12'], ['AVAX-USDT', '4h', '2023-06-01'], ['LINK-USDT', '1d', '2023-06-01'],
	]) {
		specs.push({ symbol, stream: 'ohlcv', timeframe: tf, venue: 'hyperliquid:perp', source: 'hyperliquid', market: 'perp', first: day(first), age: tfSeconds(tf) / 60 + 4 });
	}
	specs.push({ symbol: 'ETH-USDT', stream: 'ohlcv', timeframe: '1h', venue: 'okx:perp', source: 'okx', market: 'perp', first: day('2023-09-28'), age: 30_502 });
	specs.push({ symbol: 'XAU-USD', stream: 'ohlcv', timeframe: '1d', venue: 'csv:unknown', source: 'csv', market: 'unknown', first: day('2015-01-02'), age: 6_682 });
	// Needed by a gauntlet workflow and not stored yet.
	specs.push({ symbol: 'AVAX-USDT', stream: 'ohlcv', timeframe: '30m', venue: 'canonical', source: 'binanceusdm', market: 'perp', first: null, age: null });
	specCache = specs;
	return specs;
}

function consumersOf(id: string): { tier: SlaTier | null; refs: ConsumerRef[] } {
	const hits = CONSUMERS.filter((c) => c.series.includes(id));
	if (!hits.length) return { tier: null, refs: [] };
	const tier = TIER_ORDER.find((t) => hits.some((h) => h.tier === t)) ?? 'idle';
	return { tier, refs: hits.sort((a, b) => TIER_ORDER.indexOf(a.tier) - TIER_ORDER.indexOf(b.tier)).map((h) => h.ref) };
}

function planTimeframes(symbol: string): string[] | null {
	return PLAN.find(([s]) => s === symbol)?.[1] ?? null;
}

export function fixtureAssess(lastOpen: number | null, tf: string, tier: SlaTier, now: number, frozen = false): SlaAssessment {
	const rule = POLICY[tier];
	const allowed = Math.max((rule.missed_bars + 1) * tfSeconds(tf), rule.floor_minutes * 60);
	const lag = lastOpen == null ? null : Math.max(0, (now - lastOpen) / 1000);
	const ratio = lag == null ? null : +(lag / allowed).toFixed(3);
	let state: SlaState = 'fresh';
	if (frozen) state = 'frozen';
	else if (lag == null) state = 'missing';
	else if (lag > allowed * BREACH_MULTIPLIER) state = 'breach';
	else if (lag > allowed) state = 'late';
	return {
		tier,
		state,
		lag_seconds: lag == null ? null : Math.round(lag),
		allowed_seconds: allowed,
		ratio,
		last_bar_ts: lastOpen == null ? null : iso(lastOpen),
		priority: frozen ? 0 : +((ratio ?? 10) * TIER_WEIGHT[tier]).toFixed(3),
	};
}

const rowCache = new Map<number, CatalogRow[]>();

/** Every stored series (plus the live/paper/pipeline series that have no file yet). */
export function fixtureCatalogRows(now: string | number | Date = FIXTURE_NOW): CatalogRow[] {
	const nowMs = toMs(now);
	const cached = rowCache.get(nowMs);
	if (cached) return cached;
	const rows = lakeSpecs().map((spec) => buildRow(spec, nowMs));
	rowCache.clear();
	rowCache.set(nowMs, rows);
	return rows;
}

function buildRow(spec: Spec, now: number): CatalogRow {
	const id = seriesId(spec);
	const { tier: consumerTier, refs } = consumersOf(id);
	const planTfs = planTimeframes(spec.symbol);
	const inPlan = !!planTfs && (spec.stream !== 'ohlcv' || planTfs.includes(spec.timeframe));
	const tier: SlaTier = consumerTier ?? (inPlan && spec.venue === 'canonical' ? 'universe' : 'idle');
	const delistedOn = DELISTED[spec.symbol];
	const frozen = !!delistedOn || STRUCK_OUT.has(id);
	// Series a live strategy reads are kept current by the collector.
	const age = tier === 'live' && spec.age != null ? Math.min(spec.age, tfSeconds(spec.timeframe) / 60 + 3) : spec.age;
	const stepMs = tfSeconds(spec.timeframe) * 1000;
	const lastOpen = age == null ? null : barOpen(now - age * MINUTE, spec.timeframe);
	const firstOpen = spec.first == null ? null : barOpen(spec.first, spec.timeframe);
	const expected = lastOpen == null || firstOpen == null ? null : Math.max(1, Math.floor((lastOpen - firstOpen) / stepMs) + 1);
	const r = unit(id);
	const rows = expected == null ? 0 : Math.min(expected, spec.rows ?? Math.round(expected * (r < 0.8 ? 1 : 0.985 + r * 0.015)));
	const missing = expected == null ? 0 : expected - rows;
	const gapCount = missing <= 0 ? 0 : Math.max(1, Math.round(missing / (2 + Math.round(unit(`${id}:g`) * 40))));
	const largest = gapCount === 0 ? 0 : Math.max(1, Math.min(missing, Math.round(missing / gapCount) * (1 + Math.round(unit(`${id}:l`) * 3))));
	const completeness = expected == null ? null : +(rows / expected).toFixed(5);
	const assessment = fixtureAssess(lastOpen, spec.timeframe, tier, now, frozen);
	return {
		id,
		symbol: spec.symbol,
		display_symbol: display(spec.symbol),
		timeframe: spec.timeframe,
		stream: spec.stream,
		venue: spec.venue,
		source: expected == null ? null : spec.source,
		market: expected == null ? null : spec.market,
		asset_class: assetClass(spec.symbol),
		first_ts: firstOpen == null ? null : iso(firstOpen),
		last_ts: lastOpen == null ? null : iso(lastOpen),
		rows,
		expected_rows: expected,
		completeness,
		gap_count: expected == null ? null : gapCount,
		largest_gap_bars: expected == null ? null : largest,
		size_bytes: rows * BYTES_PER_ROW[spec.stream],
		quality: quality(id, completeness, gapCount, largest, now, expected == null),
		sla: assessment,
		consumers: { tier, count: refs.length, top: refs.slice(0, 5) },
		frozen,
		frozen_reason: delistedOn
			? `Delisted on Binance USD-M on ${delistedOn}`
			: STRUCK_OUT.has(id)
				? 'No new bars after 5 refreshes in a row'
				: null,
		delisted: !!delistedOn,
		synthetic_bars: 0,
		patched_bars: id === 'ohlcv:canonical:ETH-USDT:1h' ? 48 : 0,
		updated_at: lastOpen == null ? null : iso(Math.min(now, lastOpen + stepMs + 90_000)),
	};
}

function quality(id: string, completeness: number | null, gaps: number, largest: number, now: number, empty: boolean): CatalogRow['quality'] {
	if (empty) return { score: null, issues: [], computed_at: null };
	if (unit(`${id}:q`) < 0.01) return { score: null, issues: [], computed_at: null };
	let score = 100;
	const issues: string[] = [];
	if (gaps > 0) issues.push(`${gaps.toLocaleString('en-US')} gap${gaps === 1 ? '' : 's'}, largest ${largest.toLocaleString('en-US')} bar${largest === 1 ? '' : 's'}`);
	if (completeness != null && completeness < 0.98) score -= Math.min(40, (0.98 - completeness) * 200);
	if (largest >= 12) score -= 10;
	const invalid = unit(`${id}:i`) < 0.03 ? 1 + (hash(id) % 6) : 0;
	if (invalid) {
		score -= Math.min(20, invalid * 5);
		issues.push(`${invalid} bar${invalid === 1 ? '' : 's'} with high below low`);
	}
	const outliers = unit(`${id}:o`) < 0.05 ? 1 + (hash(`${id}o`) % 4) : 0;
	if (outliers) {
		score -= Math.min(10, outliers);
		issues.push(`${outliers} price jump${outliers === 1 ? '' : 's'} over 8σ`);
	}
	if (completeness != null && completeness < 0.98) issues.unshift(`${((1 - completeness) * 100).toFixed(1)}% of bars missing`);
	return { score: Math.max(0, Math.round(score * 10) / 10), issues, computed_at: iso(now - (hash(id) % 3600) * 1000) };
}

// ---------------------------------------------------------------- catalog query (as the server does it)

export interface FixtureCatalogQuery {
	q?: string;
	stream?: string[];
	venue?: string[];
	tier?: string[];
	state?: string[];
	asset_class?: string[];
	timeframe?: string[];
	sort?: string;
	order?: 'asc' | 'desc';
	limit?: number;
	offset?: number;
}

function facetsOf(rows: CatalogRow[]): CatalogFacets {
	const facets: CatalogFacets = { stream: {}, venue: {}, tier: {}, state: {}, asset_class: {}, timeframe: {} };
	for (const row of rows) {
		facets.stream[row.stream] = (facets.stream[row.stream] ?? 0) + 1;
		facets.venue[row.venue] = (facets.venue[row.venue] ?? 0) + 1;
		facets.tier[row.sla.tier] = (facets.tier[row.sla.tier] ?? 0) + 1;
		facets.state[row.sla.state] = (facets.state[row.sla.state] ?? 0) + 1;
		facets.asset_class[row.asset_class] = (facets.asset_class[row.asset_class] ?? 0) + 1;
		facets.timeframe[row.timeframe] = (facets.timeframe[row.timeframe] ?? 0) + 1;
	}
	return facets;
}

export function fixtureCatalog(query: FixtureCatalogQuery = {}, now: string | number | Date = FIXTURE_NOW): CatalogResponse {
	const all = fixtureCatalogRows(now);
	const q = (query.q ?? '').trim().toUpperCase().replace(/[/_ :]/g, '-');
	const bare = q.replace(/-/g, '');
	const has = (list: string[] | undefined, value: string) => !list?.length || list.includes(value);
	let rows = all.filter(
		(row) =>
			(!q || row.symbol.includes(q) || row.symbol.replace(/-/g, '').startsWith(bare)) &&
			has(query.stream, row.stream) &&
			has(query.venue, row.venue) &&
			has(query.tier, row.sla.tier) &&
			has(query.state, row.sla.state) &&
			has(query.asset_class, row.asset_class) &&
			has(query.timeframe, row.timeframe),
	);
	const sort = query.sort ?? 'priority';
	const key = (row: CatalogRow): number | string => {
		switch (sort) {
			case 'symbol': return `${row.symbol} ${String(tfSeconds(row.timeframe)).padStart(8, '0')} ${row.stream}`;
			case 'last_ts': return row.last_ts ?? '';
			case 'completeness': return row.completeness ?? -1;
			case 'quality': return row.quality.score ?? -1;
			case 'size': return row.size_bytes;
			case 'rows': return row.rows;
			case 'consumers': return row.consumers.count * 1000 + TIER_WEIGHT[row.sla.tier];
			default: return row.sla.priority;
		}
	};
	const dir = (query.order ?? (sort === 'symbol' ? 'asc' : 'desc')) === 'asc' ? 1 : -1;
	rows = [...rows].sort((a, b) => {
		const ka = key(a);
		const kb = key(b);
		return (ka < kb ? -1 : ka > kb ? 1 : 0) * dir || a.id.localeCompare(b.id);
	});
	const offset = query.offset ?? 0;
	const limit = query.limit ?? 200;
	return { generated_at: iso(toMs(now) - 4000), total: rows.length, rows: rows.slice(offset, offset + limit), facets: facetsOf(all) };
}

// ---------------------------------------------------------------- freshness

export function fixtureCensus(now: string | number | Date = FIXTURE_NOW, options: { limitWorst?: number; stream?: string } = {}): SlaCensus {
	const rows = fixtureCatalogRows(now).filter((row) => !options.stream || row.stream === options.stream);
	const zero = (): Record<SlaState, number> => ({ fresh: 0, late: 0, breach: 0, frozen: 0, missing: 0 });
	const states = zero();
	const byTier = Object.fromEntries(TIER_ORDER.map((t) => [t, zero()])) as Record<SlaTier, Record<SlaState, number>>;
	const byTf: Record<string, Record<SlaState, number>> = {};
	const ratios: number[] = [];
	for (const row of rows) {
		states[row.sla.state] += 1;
		byTier[row.sla.tier][row.sla.state] += 1;
		(byTf[row.timeframe] ??= zero())[row.sla.state] += 1;
		if (row.sla.ratio != null && !row.frozen) ratios.push(row.sla.ratio);
	}
	ratios.sort((a, b) => a - b);
	const pct = (p: number) => (ratios.length ? ratios[Math.min(ratios.length - 1, Math.floor(p * ratios.length))] : null);
	const worst: SlaSeriesRow[] = rows
		.filter((row) => !row.frozen && row.sla.state !== 'fresh')
		.sort((a, b) => b.sla.priority - a.sla.priority)
		.slice(0, options.limitWorst ?? 50)
		.map((row) => ({
			symbol: row.symbol,
			display_symbol: row.display_symbol,
			timeframe: row.timeframe,
			stream: row.stream,
			venue: row.venue,
			sla: row.sla,
			consumers: row.consumers,
			frozen: row.frozen,
			frozen_reason: row.frozen_reason,
		}));
	return {
		generated_at: iso(toMs(now) - 12_000),
		total: rows.length,
		states,
		by_tier: byTier,
		by_timeframe: byTf,
		worst,
		lag_ratio_p50: pct(0.5),
		lag_ratio_p95: pct(0.95),
		policy: POLICY,
		breach_multiplier: BREACH_MULTIPLIER,
	};
}

export function fixtureCollector(now: string | number | Date = FIXTURE_NOW): CollectorStatus {
	const t = toMs(now);
	return {
		enabled: true,
		tick_seconds: 60,
		last_tick: { started_at: iso(t - 42_000), finished_at: iso(t - 31_000), refreshed: 24, bars_added: 1_203, failed: 1, deferred: 12 },
		next_tick_at: iso(t + 18_000),
		queue_depth: 57,
		refreshed_last_hour: 212,
		demand_per_hour: 123,
		capacity_per_hour: 300,
		budget: [
			{ venue: 'binanceusdm', used_last_minute: 41, limit_per_minute: 120 },
			{ venue: 'binance', used_last_minute: 3, limit_per_minute: 120 },
			{ venue: 'hyperliquid', used_last_minute: 6, limit_per_minute: 60 },
			{ venue: 'deribit', used_last_minute: 1, limit_per_minute: 20 },
		],
	};
}

export function fixtureVenues(now: string | number | Date = FIXTURE_NOW): VenuesResponse {
	const t = toMs(now);
	return {
		venues: [
			{ venue: 'binanceusdm', label: 'Binance USD-M', role: 'Research candles, funding and open interest', status: 'healthy',
				last_success_at: iso(t - 31_000), last_failure_at: iso(t - 5 * 3600_000), consecutive_failures: 0, last_error: null,
				affects: 'Freshness of every canonical research series' },
			{ venue: 'binance-vision', label: 'Binance Vision', role: 'Deep history from monthly archive files', status: 'healthy',
				last_success_at: iso(t - 3 * 3600_000), last_failure_at: null, consecutive_failures: 0, last_error: null,
				affects: 'Extending history further back' },
			{ venue: 'hyperliquid', label: 'Hyperliquid', role: 'Execution venue for live strategies', status: 'healthy',
				last_success_at: iso(t - 12_000), last_failure_at: iso(t - 26 * 3600_000), consecutive_failures: 0, last_error: null,
				affects: 'Venue candles the live fleet trades on' },
			{ venue: 'okx', label: 'OKX liquidations', role: 'Liquidation feed (WebSocket)', status: 'degraded',
				last_success_at: iso(t - 7 * 60_000), last_failure_at: iso(t - 40_000), consecutive_failures: 3,
				last_error: 'WebSocket closed (1006); reconnecting in 30 s',
				affects: 'Liquidation columns for strategies that read them' },
			{ venue: 'deribit', label: 'Deribit IV', role: 'BTC and ETH implied volatility (DVOL)', status: 'healthy',
				last_success_at: iso(t - 22 * 60_000), last_failure_at: null, consecutive_failures: 0, last_error: null,
				affects: 'IV columns (S10869 reads ETH IV)' },
		],
	};
}

// ---------------------------------------------------------------- jobs

function job(partial: Partial<DataJob> & Pick<DataJob, 'id' | 'kind' | 'title' | 'status'>): DataJob {
	return {
		origin: 'user',
		lane: 'binance',
		routine: false,
		params: {},
		series: [],
		progress: { done: 0, total: null, unit: null },
		message: null,
		result: null,
		error: null,
		attempts: partial.status === 'queued' ? 0 : 1,
		parent_id: null,
		cancel_requested: false,
		retryable: false,
		created_at: partial.created_at ?? FIXTURE_NOW,
		started_at: null,
		finished_at: null,
		updated_at: partial.created_at ?? FIXTURE_NOW,
		...partial,
	};
}

export function fixtureJobs(now: string | number | Date = FIXTURE_NOW): DataJob[] {
	const t = toMs(now);
	const ago = (minutes: number) => iso(t - minutes * MINUTE);
	const jobs: DataJob[] = [
		job({ id: 'dj-7f3a91c20b11', kind: 'history_extend', title: 'Extend history · XRP-USDT from Binance Vision', status: 'running', lane: 'binance-vision',
			series: [{ symbol: 'XRP-USDT', timeframe: '1m', stream: 'ohlcv', venue: 'canonical' }], progress: { done: 23, total: 64, unit: 'months' },
			message: 'Importing 2021-11', created_at: ago(7), started_at: ago(6.5), updated_at: ago(0.1) }),
		job({ id: 'dj-51be0d9a4c02', kind: 'download', title: 'Download SUI-USDT 15m · last 3 years', status: 'running',
			series: [{ symbol: 'SUI-USDT', timeframe: '15m', stream: 'ohlcv', venue: 'canonical' }], progress: { done: 61_200, total: 105_120, unit: 'bars' },
			params: { symbol: 'SUI-USDT', timeframe: '15m', venue: 'canonical', history: { mode: 'days', days: 1095 } },
			created_at: ago(3), started_at: ago(2.8), updated_at: ago(0.05) }),
		job({ id: 'dj-0c9d2e77aa31', kind: 'gap_repair', title: 'Repair gaps · ADA-BTC 1h', status: 'queued', lane: 'binance',
			series: [{ symbol: 'ADA-BTC', timeframe: '1h', stream: 'ohlcv', venue: 'canonical' }], created_at: ago(1) }),
		job({ id: 'dj-e4412f0b9d77', kind: 'download', title: 'Download HBAR-USDT 1h · all available', status: 'failed',
			series: [{ symbol: 'HBAR-USDT', timeframe: '1h', stream: 'ohlcv', venue: 'canonical' }], progress: { done: 18_000, total: 52_400, unit: 'bars' },
			error: { code: 'rate_limited', message: 'Binance returned HTTP 429: request weight limit exceeded' }, retryable: true,
			created_at: ago(190), started_at: ago(189), finished_at: ago(184), updated_at: ago(184) }),
		job({ id: 'dj-3a1f6c5e2d90', kind: 'universe_seed', title: 'Research universe seed', status: 'interrupted', origin: 'universe', lane: 'binance-vision',
			error: { code: 'backend_restarted', message: 'backend restarted mid-seed; restart the seed (it resumes)' }, retryable: true,
			progress: { done: 136, total: 180, unit: 'series' }, created_at: '2026-07-06T11:43:35Z', started_at: '2026-07-06T11:43:36Z',
			finished_at: '2026-07-06T14:02:10Z', updated_at: '2026-07-06T14:02:10Z' }),
		job({ id: 'dj-9b77e1d04f5a', kind: 'download', title: 'Download AAVE-USDT 4h from Binance USD-M', status: 'succeeded',
			series: [{ symbol: 'AAVE-USDT', timeframe: '4h', stream: 'ohlcv', venue: 'canonical' }], progress: { done: 13_039, total: 13_039, unit: 'bars' },
			result: { bars: 13_039 }, created_at: ago(744), started_at: ago(744), finished_at: ago(743), updated_at: ago(743) }),
		job({ id: 'dj-6e02b8c1f3d4', kind: 'tail_refresh', title: 'Refresh SOL-USDT 15m', status: 'succeeded', origin: 'strategy:S04928',
			series: [{ symbol: 'SOL-USDT', timeframe: '15m', stream: 'ohlcv', venue: 'canonical' }], progress: { done: 2, total: 2, unit: 'bars' },
			result: { bars_added: 2 }, created_at: ago(95), started_at: ago(95), finished_at: ago(95), updated_at: ago(95) }),
		job({ id: 'dj-2d4c7a9e1b63', kind: 'csv_import', title: 'Import XAUUSD_daily.csv → XAU-USD 1d', status: 'succeeded', lane: 'local',
			series: [{ symbol: 'XAU-USD', timeframe: '1d', stream: 'ohlcv', venue: 'csv:unknown' }], progress: { done: 3_020, total: 3_020, unit: 'rows' },
			result: { rows_written: 3_020 }, created_at: ago(1_560), started_at: ago(1_560), finished_at: ago(1_559), updated_at: ago(1_559) }),
		job({ id: 'dj-c81f5e0a7b29', kind: 'download', title: 'Download PEPE-USDT 1m · all available', status: 'cancelled',
			series: [{ symbol: 'PEPE-USDT', timeframe: '1m', stream: 'ohlcv', venue: 'canonical' }], progress: { done: 41_000, total: 310_000, unit: 'bars' },
			message: 'cancelled by user', cancel_requested: true, retryable: true, created_at: ago(2_100), started_at: ago(2_100), finished_at: ago(2_096), updated_at: ago(2_096) }),
		job({ id: 'dj-47ad90c3e5f1', kind: 'reclaim', title: 'Move 12 stale temp files to the trash', status: 'succeeded', lane: 'local',
			result: { moved: 12, bytes: 35_651_584 }, created_at: ago(3_000), started_at: ago(3_000), finished_at: ago(2_999), updated_at: ago(2_999) }),
	];
	for (let i = 0; i < 24; i++) {
		const start = t - (i * 60 + 42) * 1000;
		const refreshed = 18 + (hash(`tick${i}`) % 14);
		const failed = i % 7 === 3 ? 1 : 0;
		jobs.push(job({
			id: `dj-tick${String(1000 + i)}`,
			kind: 'sla_collect',
			title: 'Collection tick',
			status: 'succeeded',
			origin: 'sla',
			lane: 'local',
			routine: true,
			result: { refreshed, bars_added: refreshed * 38 + (hash(`b${i}`) % 300), failed, deferred: 8 + (i % 5) },
			message: `refreshed ${refreshed} series`,
			created_at: iso(start),
			started_at: iso(start),
			finished_at: iso(start + 11_000),
			updated_at: iso(start + 11_000),
		}));
	}
	return jobs.sort((a, b) => b.created_at.localeCompare(a.created_at));
}

export function fixtureJobsSummary(now: string | number | Date = FIXTURE_NOW): DataJobSummary {
	const jobs = fixtureJobs(now);
	const dayAgo = iso(toMs(now) - DAY);
	const recent = jobs.filter((j) => !j.routine && j.created_at >= dayAgo);
	return {
		running: jobs.filter((j) => j.status === 'running').length,
		queued: jobs.filter((j) => j.status === 'queued').length,
		failed_24h: recent.filter((j) => j.status === 'failed' || j.status === 'interrupted').length,
		succeeded_24h: recent.filter((j) => j.status === 'succeeded').length,
		last_routine: jobs.find((j) => j.routine) ?? null,
	};
}

// ---------------------------------------------------------------- storage, trash, identity, universe

const MB = 1024 * 1024;

export function fixtureStorage(now: string | number | Date = FIXTURE_NOW): StorageInventory {
	const t = toMs(now);
	const rows = fixtureCatalogRows(now).filter((row) => row.rows > 0);
	const byStream = new Map<string, { bytes: number; files: number }>();
	for (const row of rows) {
		const entry = byStream.get(row.stream) ?? { bytes: 0, files: 0 };
		entry.bytes += row.size_bytes;
		entry.files += 1;
		byStream.set(row.stream, entry);
	}
	const lakeBytes = rows.reduce((sum, row) => sum + row.size_bytes, 0);
	const backups = rows
		.filter((row) => row.stream === 'ohlcv' && row.venue === 'canonical' && row.source === 'binanceusdm')
		.slice(0, 106)
		.map((row, i) => ({
			id: `bak-${i}`,
			path: `ohlcv/${row.symbol}/${row.timeframe}.parquet.spotmix.bak`,
			bytes: Math.round((641 * MB) / 106 + ((hash(row.id) % 2000) - 1000) * 1024),
			modified_at: '2026-07-02T09:14:00Z',
		}));
	const legacyNames = [
		['funding_btc.parquet', 18], ['binance_ohlcv.db', 96], ['MATICUSDT_4h.csv', 2], ['btc_1h_features.parquet', 31], ['oi_snapshot_0322.json', 1],
		['research_eth_regimes.parquet', 12], ['sentiment_cache.db', 24], ['basis_legacy.parquet', 9], ['ETHUSDT_15m.csv', 14], ['liq_test.parquet', 3],
	] as const;
	const legacy = legacyNames.map(([name, mb], i) => ({ id: `legacy-${i}`, path: name, bytes: mb * MB, modified_at: iso(ms('2026-03-01T00:00:00Z') + i * 11 * DAY) }));
	const tmp = Array.from({ length: 12 }, (_, i) => ({
		id: `tmp-${i}`, path: `ohlcv/${['BTC-USDT', 'ETH-USDT', 'SOL-USDT'][i % 3]}/${['1m', '5m', '15m', '1h'][i % 4]}.parquet.${(hash(`t${i}`) % 90000) + 10000}.tmp`,
		bytes: Math.round(2.9 * MB) + i * 4096, modified_at: iso(t - (3 + i) * DAY),
	}));
	const reclaimable: ReclaimGroup[] = [
		{ kind: 'backups', label: 'Reconcile backups', description: 'Copies written by the 2026-07-02 spot→perp reconcile (*.spotmix.bak). Every series they back up was re-downloaded from the perp venue and has passed its checks since.',
			bytes: backups.reduce((s, i) => s + i.bytes, 0), count: backups.length, items: backups, safe: true },
		{ kind: 'revisions', label: 'Old revision log entries', description: 'Point-in-time history of restated bars older than 30 days. Restatements inside a saved verdict’s window are kept.',
			bytes: 212 * MB, count: 96, items: Array.from({ length: 12 }, (_, i) => ({ id: `rev-${i}`, path: `revisions/2026-0${3 + (i % 5)}/${['BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'XRP-USDT'][i % 4]}_1h.parquet`, bytes: Math.round(2.2 * MB), modified_at: iso(ms('2026-03-02T00:00:00Z') + i * 9 * DAY) })), safe: true },
		{ kind: 'legacy_root', label: 'Legacy files in the data folder', description: 'Files from before the lake layout (Mar–Jul research artifacts). Nothing reads them.',
			bytes: legacy.reduce((s, i) => s + i.bytes, 0), count: legacy.length, items: legacy, safe: false },
		{ kind: 'orphan_tmp', label: 'Stale temporary files', description: 'Half-written files left by interrupted writes. The series they belong to are intact.',
			bytes: tmp.reduce((s, i) => s + i.bytes, 0), count: tmp.length, items: tmp, safe: true },
		{ kind: 'stray_dirs', label: 'Folders that are not a symbol', description: 'Folders in the lake whose name is not an instrument.',
			bytes: 61_440, count: 1, items: [{ id: 'stray-0', path: 'ohlcv/RETRY/', bytes: 61_440, modified_at: '2026-06-21T08:02:00Z', note: 'Written by a failed retry path' }], safe: false },
		{ kind: 'empty_dirs', label: 'Empty folders', description: 'Folders with no files in them.',
			bytes: 0, count: 7, items: ['XAU-USD', 'BTC-PERP', 'ETHUSDT', 'SOL', 'LUNA-USDT', 'FTT-USDT', 'SRM-USDT'].map((name, i) => ({ id: `empty-${i}`, path: `ohlcv/${name}/`, bytes: 0, modified_at: null })), safe: true },
	];
	return {
		generated_at: iso(t - 20_000),
		data_root: '~/.forven/data',
		disk: { free_bytes: 212 * 1024 * MB, total_bytes: 953 * 1024 * MB, min_free_gb: 5 },
		lake: { bytes: lakeBytes, files: rows.length + 69, series: rows.length },
		by_stream: [...byStream.entries()].map(([stream, v]) => ({ stream, ...v })).sort((a, b) => b.bytes - a.bytes),
		top_series: [...rows].sort((a, b) => b.size_bytes - a.size_bytes).slice(0, 12)
			.map((row) => ({ symbol: row.symbol, timeframe: row.timeframe, stream: row.stream, venue: row.venue, bytes: row.size_bytes })),
		reclaimable,
		trash: { items: 2, bytes: 18 * MB + 170_000, oldest: iso(t - 3 * DAY), retention_days: 7 },
		revisions: { bytes: 357 * MB, files: 162, oldest: '2026-03-02T00:00:00Z', keep_days: 30, prunable_bytes: 212 * MB },
	};
}

export function fixtureTrash(now: string | number | Date = FIXTURE_NOW): TrashResponse {
	const t = toMs(now);
	return {
		retention_days: 7,
		bytes: 18 * MB + 170_000,
		items: [
			{ id: 'tr-0192', kind: 'series', label: 'USDT-TRY 1h', original_path: 'ohlcv/USDT-TRY/1h.parquet', bytes: 170_000,
				deleted_at: iso(t - 3 * DAY), purge_after: iso(t + 4 * DAY), reason: 'Deleted from the catalog',
				series: { symbol: 'USDT-TRY', timeframe: '1h', stream: 'ohlcv', venue: 'canonical' } },
			{ id: 'tr-0187', kind: 'legacy', label: 'funding_btc.parquet', original_path: 'funding_btc.parquet', bytes: 18 * MB,
				deleted_at: iso(t - 26 * 3600_000), purge_after: iso(t + 6 * DAY - 2 * 3600_000), reason: 'Reclaim: legacy files', series: null },
		],
	};
}

export function fixtureIdentityAudit(now: string | number | Date = FIXTURE_NOW): IdentityAuditResponse {
	return {
		generated_at: iso(toMs(now) - 60_000),
		issues: [
			{ kind: 'alias_duplicate', path: 'ohlcv/BTCUSD/', symbol: 'BTCUSD', detail: 'Same instrument as BTC-USD (Binance spot BTC/USD), written by a path that skipped symbol normalisation (last write today 13:19 UTC).',
				related: ['ohlcv/BTC-USD/'], bytes: 164_000, suggestion: 'Merge into BTC-USD, then retire BTCUSD.' },
			{ kind: 'alias_duplicate', path: 'ohlcv/BTC-USD/', symbol: 'BTC-USD', detail: 'Binance spot BTC/USD, a different instrument from the BTC-USDT perp the research lake uses.',
				related: ['ohlcv/BTC-USDT/', 'ohlcv/BTCUSD/'], bytes: 690_000, suggestion: 'Keep only if a strategy researches the USD spot pair.' },
			{ kind: 'stray_dir', path: 'ohlcv/RETRY/', symbol: 'RETRY', detail: 'Not an instrument. Created by a failed retry path on 2026-06-21.',
				related: [], bytes: 61_440, suggestion: 'Move to the trash (Storage → Folders that are not a symbol).' },
			{ kind: 'delisted_collected', path: 'ohlcv/MULTI-USDT/', symbol: 'MULTI-USDT', detail: 'Delisted 2023-07-14; the old catch-up kept rewriting its files until today. Now frozen.',
				related: [], bytes: 415_000, suggestion: 'Keep for survivorship-free research, or delete.' },
			{ kind: 'unknown_symbol', path: 'ohlcv/USDT-USD/', symbol: 'USDT-USD', detail: 'Stablecoin vs fiat spot pair; not in the USD-M registry.',
				related: ['ohlcv/USDT-TRY/'], bytes: 216_000, suggestion: 'Keep if you research FX pairs; otherwise delete.' },
			{ kind: 'empty_dir', path: 'ohlcv/XAU-USD/', symbol: 'XAU-USD', detail: 'Empty folder; the CSV import stored XAU-USD as a venue series instead.',
				related: ['ohlcv/source=csv/market=unknown/XAU-USD/'], bytes: 0, suggestion: 'Remove (Storage → Empty folders).' },
			{ kind: 'unstamped', path: 'ohlcv/ALT-BTC/1h.parquet', symbol: 'ALT-BTC', detail: 'No forven_source stamp: written before provenance stamping.',
				related: [], bytes: 148_000, suggestion: 'Re-download to stamp it, or leave it frozen.' },
		],
	};
}

export function fixturePlanDiff(now: string | number | Date = FIXTURE_NOW): UniversePlanDiff {
	const stored = new Set(fixtureCatalogRows(now).filter((r) => r.stream === 'ohlcv' && r.venue === 'canonical' && r.rows > 0).map((r) => `${r.symbol}:${r.timeframe}`));
	const missing: UniversePlanDiff['missing'] = [];
	let planned = 0;
	PLAN.forEach(([symbol, tfs], rank) => {
		planned += tfs.length;
		const absent = tfs.filter((tf) => !stored.has(`${symbol}:${tf}`));
		if (absent.length) missing.push({ symbol, rank, timeframes: absent, asset_class: assetClass(symbol) });
	});
	const missingCount = missing.reduce((s, m) => s + m.timeframes.length, 0);
	const seed = fixtureJobs(now).find((j) => j.kind === 'universe_seed') ?? null;
	return {
		enabled: true,
		size: 50,
		asset_classes: ['crypto', 'tradfi', 'stock', 'etf'],
		planned_series: planned,
		present_series: planned - missingCount,
		missing,
		extra: [{ symbol: 'TIA-USDT', timeframes: ['1h', '4h', '1d'] }, { symbol: 'OP-USDT', timeframes: ['1h', '4h'] }, { symbol: 'DOT-USDT', timeframes: ['1h', '4h', '1d'] }],
		seed_job: seed,
	};
}

// ---------------------------------------------------------------- data log

export function fixtureLogEntries(now: string | number | Date = FIXTURE_NOW): DataLogEntry[] {
	const t = toMs(now);
	const at = (minutes: number) => iso(t - minutes * MINUTE);
	const entries: DataLogEntry[] = [
		{ id: 'log-u1', ts: at(3), level: 'info', category: 'user', action: 'download', message: 'Started download SUI-USDT 15m · last 3 years', symbol: 'SUI-USDT', timeframe: '15m', job_id: 'dj-51be0d9a4c02', origin: 'user', detail: {} },
		{ id: 'log-u2', ts: at(7), level: 'info', category: 'user', action: 'history_extend', message: 'Started history extension for XRP-USDT (Binance Vision)', symbol: 'XRP-USDT', timeframe: null, job_id: 'dj-7f3a91c20b11', origin: 'user', detail: {} },
		{ id: 'log-i1', ts: at(38), level: 'warning', category: 'incident', action: 'sla_late', message: 'SOL-USDT 15m is late for paper strategy S04928: 52 min behind, 45 min allowed', symbol: 'SOL-USDT', timeframe: '15m', job_id: null, origin: 'sla', detail: { tier: 'paper' } },
		{ id: 'log-i2', ts: at(41), level: 'warning', category: 'incident', action: 'venue_degraded', message: 'OKX liquidation feed disconnected 3 times in 10 minutes', symbol: null, timeframe: null, job_id: null, origin: 'system', detail: { venue: 'okx' } },
		{ id: 'log-i3', ts: at(184), level: 'error', category: 'incident', action: 'job_failed', message: 'Download HBAR-USDT 1h failed: Binance returned HTTP 429 (rate limited)', symbol: 'HBAR-USDT', timeframe: '1h', job_id: 'dj-e4412f0b9d77', origin: 'user', detail: { code: 'rate_limited' } },
		{ id: 'log-u3', ts: at(190), level: 'info', category: 'user', action: 'download', message: 'Started download HBAR-USDT 1h · all available', symbol: 'HBAR-USDT', timeframe: '1h', job_id: 'dj-e4412f0b9d77', origin: 'user', detail: {} },
		{ id: 'log-i4', ts: at(530), level: 'warning', category: 'incident', action: 'sla_frozen', message: 'ALT-BTC 1h frozen: no new bars after 5 refreshes in a row', symbol: 'ALT-BTC', timeframe: '1h', job_id: null, origin: 'sla', detail: {} },
		{ id: 'log-u4', ts: at(743), level: 'info', category: 'user', action: 'download', message: 'Downloaded AAVE-USDT 4h from Binance USD-M · 13,039 bars', symbol: 'AAVE-USDT', timeframe: '4h', job_id: 'dj-9b77e1d04f5a', origin: 'user', detail: { bars: 13039 } },
		{ id: 'log-u5', ts: at(1_559), level: 'info', category: 'user', action: 'csv_import', message: 'Imported XAUUSD_daily.csv into XAU-USD 1d (venue series csv:unknown) · 3,020 rows', symbol: 'XAU-USD', timeframe: '1d', job_id: 'dj-2d4c7a9e1b63', origin: 'user', detail: {} },
		{ id: 'log-u6', ts: at(2_096), level: 'warning', category: 'user', action: 'download', message: 'Cancelled download PEPE-USDT 1m after 41,000 of 310,000 bars', symbol: 'PEPE-USDT', timeframe: '1m', job_id: 'dj-c81f5e0a7b29', origin: 'user', detail: {} },
		{ id: 'log-u7', ts: at(4_320), level: 'info', category: 'user', action: 'delete', message: 'Moved USDT-TRY 1h to the trash (purged after 7 days)', symbol: 'USDT-TRY', timeframe: '1h', job_id: null, origin: 'user', detail: {} },
	];
	for (let i = 0; i < 180; i++) {
		const refreshed = 18 + (hash(`tick${i}`) % 14);
		const failed = i % 7 === 3 ? 1 : 0;
		entries.push({
			id: `log-r${i}`,
			ts: iso(t - (i * 60 + 42) * 1000 + 11_000),
			level: failed ? 'warning' : 'info',
			category: 'routine',
			action: 'sla_collect',
			message: `Collection: refreshed ${refreshed} series, +${(refreshed * 38 + (hash(`b${i}`) % 300)).toLocaleString('en-US')} bars${failed ? ', 1 failed (MATIC-USDT funding: delisted)' : ''}`,
			symbol: null,
			timeframe: null,
			job_id: `dj-tick${1000 + i}`,
			origin: 'sla',
			detail: { refreshed, failed },
			children: refreshed,
		});
	}
	return entries.sort((a, b) => b.ts.localeCompare(a.ts));
}

export function fixtureLog(
	query: { category?: string[]; level?: string[]; symbol?: string; action?: string; since?: string; until?: string; q?: string; limit?: number; offset?: number } = {},
	now: string | number | Date = FIXTURE_NOW,
): DataLogResponse {
	const q = (query.q ?? '').toLowerCase();
	const symbol = (query.symbol ?? '').toUpperCase().replace('/', '-');
	const entries = fixtureLogEntries(now).filter(
		(e) =>
			(!query.category?.length || query.category.includes(e.category)) &&
			(!query.level?.length || query.level.includes(e.level)) &&
			(!symbol || (e.symbol ?? '').includes(symbol)) &&
			(!query.action || e.action === query.action) &&
			(!query.since || e.ts >= query.since) &&
			(!query.until || e.ts <= query.until) &&
			(!q || e.message.toLowerCase().includes(q)),
	);
	const offset = query.offset ?? 0;
	return { total: entries.length, entries: entries.slice(offset, offset + (query.limit ?? 50)) };
}

// ---------------------------------------------------------------- one series

const PRICE_NOW: Record<string, number> = {
	BTC: 112_400, ETH: 4_180, SOL: 208, XRP: 2.84, BNB: 905, DOGE: 0.24, ADA: 0.81, AVAX: 29.5, LINK: 22.4, XAU: 3_780, XAG: 44.1,
};

function priceAt(symbol: string, t: number): number {
	const base = symbol.split('-')[0];
	const h = hash(symbol);
	const years = (t - Date.UTC(2020, 0, 1)) / (365.25 * DAY);
	const shape = (y: number) =>
		0.9 * Math.sin(y * 1.7 + (h % 7)) + 0.35 * Math.sin(y * 9.3 + (h % 11)) + 0.12 * Math.sin(y * 57 + (h % 13)) +
		0.04 * Math.sin(y * 410 + (h % 17)) + 0.012 * Math.sin(y * 3_100 + (h % 19)) + 0.004 * Math.sin(y * 21_000 + (h % 23)) + 0.28 * y;
	const target = PRICE_NOW[base] ?? 0.2 + (h % 4000) / 100;
	return target * Math.exp(shape(years) - shape((ms(FIXTURE_NOW) - Date.UTC(2020, 0, 1)) / (365.25 * DAY)));
}

function findRow(ref: { symbol: string; timeframe: string; stream?: string; venue?: string }, now: string | number | Date): CatalogRow | undefined {
	const stream = ref.stream ?? 'ohlcv';
	const venue = ref.venue ?? 'canonical';
	return fixtureCatalogRows(now).find((r) => r.symbol === ref.symbol && r.timeframe === ref.timeframe && r.stream === stream && r.venue === venue);
}

function gapSpans(row: CatalogRow): GapSpan[] {
	const count = row.gap_count ?? 0;
	if (!count || !row.first_ts || !row.last_ts) return [];
	const step = tfSeconds(row.timeframe) * 1000;
	const first = ms(row.first_ts);
	const span = ms(row.last_ts) - first;
	const missing = (row.expected_rows ?? 0) - row.rows;
	const spans: GapSpan[] = [];
	let left = missing;
	for (let i = 0; i < Math.min(count, 200); i++) {
		const bars = i === 0 ? Math.min(left, row.largest_gap_bars ?? 1) : Math.max(1, Math.min(left, Math.round((missing - (row.largest_gap_bars ?? 0)) / Math.max(1, count - 1))));
		if (bars <= 0) break;
		left -= bars;
		const start = first + Math.floor((unit(`${row.id}:gap${i}`) * 0.96 + 0.02) * span / step) * step;
		spans.push({ start: iso(start), end: iso(start + (bars - 1) * step), bars, kind: i % 9 === 4 ? 'unfillable' : 'missing' });
	}
	return spans.sort((a, b) => b.bars - a.bars);
}

function monthMap(row: CatalogRow, gaps: GapSpan[]): MonthCell[] {
	if (!row.first_ts || !row.last_ts) return [];
	const step = tfSeconds(row.timeframe) * 1000;
	const first = ms(row.first_ts);
	const last = ms(row.last_ts);
	const cells: MonthCell[] = [];
	const cursor = new Date(Date.UTC(new Date(first).getUTCFullYear(), new Date(first).getUTCMonth(), 1));
	while (cursor.getTime() <= last) {
		const monthStart = cursor.getTime();
		const next = Date.UTC(cursor.getUTCFullYear(), cursor.getUTCMonth() + 1, 1);
		const from = Math.max(monthStart, first);
		const to = Math.min(next - 1, last);
		const expected = Math.max(0, Math.floor((to - from) / step) + 1);
		let missing = 0;
		for (const gap of gaps) {
			const gs = ms(gap.start);
			const ge = ms(gap.end);
			const overlap = Math.min(ge, to) - Math.max(gs, from);
			if (overlap >= 0) missing += Math.floor(overlap / step) + 1;
		}
		const month = `${cursor.getUTCFullYear()}-${String(cursor.getUTCMonth() + 1).padStart(2, '0')}`;
		cells.push({
			month,
			expected,
			present: Math.max(0, expected - missing),
			synthetic: 0,
			patched: row.patched_bars && month === '2021-05' ? row.patched_bars : 0,
			restated: row.symbol === 'BTC-USDT' && row.stream === 'ohlcv' && (month === '2024-03' || month === '2025-11') ? (month === '2024-03' ? 12 : 3) : 0,
		});
		cursor.setUTCMonth(cursor.getUTCMonth() + 1);
	}
	return cells;
}

const STREAM_COLUMNS: Record<DataStream, string[]> = {
	ohlcv: ['open', 'high', 'low', 'close', 'volume'],
	funding: ['funding_rate'],
	oi: ['open_interest', 'open_interest_value'],
	basis: ['basis_pct', 'mark_price', 'index_price'],
	iv: ['iv'],
	ls_ratio: ['long_short_ratio', 'long_account', 'short_account'],
	taker: ['taker_buy_sell_ratio', 'taker_buy_volume', 'taker_sell_volume'],
	liquidations: ['liq_long_usd', 'liq_short_usd'],
};

export function fixtureSeriesDetail(
	ref: { symbol: string; timeframe: string; stream?: string; venue?: string },
	now: string | number | Date = FIXTURE_NOW,
): SeriesDetail | null {
	const row = findRow(ref, now);
	if (!row) return null;
	const all = fixtureCatalogRows(now);
	const gaps = gapSpans(row);
	const siblings = all.filter((r) => r.symbol === row.symbol && r.stream !== row.stream && r.venue === 'canonical');
	const streams: StreamSummary[] = [];
	for (const stream of ['funding', 'oi', 'basis', 'iv', 'ls_ratio', 'taker', 'liquidations', 'ohlcv'] as DataStream[]) {
		const candidates = siblings.filter((r) => r.stream === stream);
		const pick = candidates.find((r) => r.timeframe === row.timeframe) ?? candidates.find((r) => r.timeframe === '1h') ?? candidates[0];
		if (!pick) continue;
		streams.push({ stream, timeframe: pick.timeframe, venue: pick.venue, rows: pick.rows, first_ts: pick.first_ts, last_ts: pick.last_ts, size_bytes: pick.size_bytes, sla: pick.sla, columns: STREAM_COLUMNS[stream] });
	}
	const consumerRefs = CONSUMERS.filter((c) => c.series.includes(row.id));
	const consumers: ConsumerDetail[] = consumerRefs.map((c) => ({
		...c.ref,
		gate:
			c.ref.kind === 'strategy'
				? row.sla.state === 'fresh' || row.sla.state === 'late' && c.tier !== 'pipeline'
					? { ok: true, reasons: [] }
					: { ok: false, reasons: row.sla.state === 'missing' ? ['No stored bars for this timeframe'] : [`Last bar is ${Math.round((row.sla.lag_seconds ?? 0) / 60)} min old; the gate allows ${Math.round(row.sla.allowed_seconds / 60)} min`] }
				: null,
	}));
	const jobs = fixtureJobs(now).filter((j) => j.series.some((s) => s.symbol === row.symbol && (!s.timeframe || s.timeframe === row.timeframe))).slice(0, 5);
	const venues = [...new Set(all.filter((r) => r.symbol === row.symbol && r.timeframe === row.timeframe && r.stream === row.stream).map((r) => r.venue))];
	return {
		...row,
		month_map: monthMap(row, gaps),
		gaps: gaps.slice(0, 200),
		gaps_total: gaps.length,
		streams,
		provenance: {
			source: row.source,
			market: row.market,
			venue: row.venue,
			stamped_symbol: row.source === 'binanceusdm' ? `${row.display_symbol}:${row.symbol.split('-')[1] ?? 'USDT'}` : row.display_symbol,
			stamped_at: row.updated_at,
			synthetic_ranges: [],
			patched_ranges: row.patched_bars ? [['2021-05-19T00:00:00Z', '2021-05-20T23:00:00Z']] : [],
			restatements: row.symbol === 'BTC-USDT' && row.stream === 'ohlcv'
				? [
					{ observed_at: '2025-11-04T02:10:00Z', rows: 3, first_ts: '2025-11-03T21:00:00Z', last_ts: '2025-11-03T23:00:00Z' },
					{ observed_at: '2024-03-12T06:44:00Z', rows: 12, first_ts: '2024-03-11T08:00:00Z', last_ts: '2024-03-11T19:00:00Z' },
				]
				: [],
		},
		consumers_detail: consumers,
		recent_jobs: jobs,
		venues_available: venues,
	};
}

const BUCKETS = ['1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w'];

export function fixtureBars(
	ref: { symbol: string; timeframe: string; venue?: string },
	window: { start?: string | null; end?: string | null; max_points?: number } = {},
	now: string | number | Date = FIXTURE_NOW,
): BarsResponse | null {
	const row = findRow({ ...ref, stream: 'ohlcv' }, now);
	if (!row || !row.first_ts || !row.last_ts) return null;
	const step = tfSeconds(row.timeframe) * 1000;
	const first = ms(row.first_ts);
	const last = ms(row.last_ts);
	const start = Math.max(first, window.start ? barOpen(ms(window.start), row.timeframe) : first);
	const end = Math.min(last, window.end ? ms(window.end) : last);
	const maxPoints = window.max_points ?? 1500;
	const inRange = end < start ? 0 : Math.floor((end - start) / step) + 1;
	let bucket = row.timeframe;
	for (const candidate of BUCKETS) {
		if (tfSeconds(candidate) < tfSeconds(row.timeframe)) continue;
		bucket = candidate;
		if (Math.ceil(inRange * tfSeconds(row.timeframe) / tfSeconds(candidate)) <= maxPoints) break;
	}
	const raw = bucket === row.timeframe;
	const size = tfSeconds(bucket) * 1000;
	const gaps = raw ? gapSpans(row).map((g) => [ms(g.start), ms(g.end)] as const) : [];
	const bars: Bar[] = [];
	for (let t = barOpen(start, bucket); t <= end && bars.length < maxPoints + 2; t += size) {
		if (gaps.some(([gs, ge]) => t >= gs && t <= ge)) continue;
		const open = priceAt(row.symbol, t);
		const close = priceAt(row.symbol, t + size);
		const wiggle = 0.0025 * Math.sqrt(size / 3_600_000) * (0.5 + unit(`${row.symbol}${t}`));
		bars.push({ t: iso(t), o: open, h: Math.max(open, close) * (1 + wiggle), l: Math.min(open, close) * (1 - wiggle), c: close, v: Math.round(800 + unit(`v${t}`) * 4_000) * (size / step) });
	}
	return { symbol: row.symbol, timeframe: row.timeframe, venue: row.venue, start: iso(start), end: iso(end), resolution: raw ? 'raw' : bucket, raw, total_in_range: inRange, bars };
}

export function fixtureGaps(ref: { symbol: string; timeframe: string; stream?: string; venue?: string }, page: { limit?: number; offset?: number } = {}, now: string | number | Date = FIXTURE_NOW): GapsResponse {
	const row = findRow(ref, now);
	const gaps = row ? gapSpans(row) : [];
	const offset = page.offset ?? 0;
	return { total: gaps.length, gaps: gaps.slice(offset, offset + (page.limit ?? 50)) };
}

export function fixtureRows(
	ref: { symbol: string; timeframe: string; stream?: string; venue?: string },
	query: { start?: string | null; end?: string | null; limit?: number; offset?: number } = {},
	now: string | number | Date = FIXTURE_NOW,
): RowsResponse {
	const row = findRow(ref, now);
	const stream = (ref.stream ?? 'ohlcv') as DataStream;
	const columns = ['timestamp', ...STREAM_COLUMNS[stream]];
	if (!row || !row.first_ts || !row.last_ts) return { total: 0, columns, rows: [] };
	const step = tfSeconds(row.timeframe) * 1000;
	const start = Math.max(ms(row.first_ts), query.start ? barOpen(ms(query.start), row.timeframe) : ms(row.last_ts) - 199 * step);
	const end = Math.min(ms(row.last_ts), query.end ? ms(query.end) : ms(row.last_ts));
	const total = end < start ? 0 : Math.floor((end - start) / step) + 1;
	const offset = query.offset ?? 0;
	const limit = query.limit ?? 100;
	const rows: RowsResponse['rows'] = [];
	for (let i = offset; i < Math.min(total, offset + limit); i++) {
		const t = start + i * step;
		const p = priceAt(row.symbol, t);
		const q = priceAt(row.symbol, t + step);
		const values: Record<DataStream, Array<number>> = {
			ohlcv: [p, Math.max(p, q) * 1.0012, Math.min(p, q) * 0.9988, q, Math.round(900 + unit(`v${t}`) * 3_000)],
			funding: [+((Math.sin(t / 5e8) * 0.0001 + 0.00005)).toFixed(7)],
			oi: [Math.round(80_000 + unit(`oi${t}`) * 9_000), Math.round((80_000 + unit(`oi${t}`) * 9_000) * p)],
			basis: [+(Math.sin(t / 7e8) * 0.08 + 0.02).toFixed(4), q * 1.0002, q],
			iv: [+(48 + Math.sin(t / 9e8) * 9).toFixed(2)],
			ls_ratio: [+(1.1 + Math.sin(t / 6e8) * 0.4).toFixed(3), 0.52, 0.48],
			taker: [+(1 + Math.sin(t / 4e8) * 0.15).toFixed(3), 1200, 1100],
			liquidations: [Math.round(unit(`ll${t}`) * 90_000), Math.round(unit(`ls${t}`) * 70_000)],
		};
		rows.push(Object.fromEntries([['timestamp', iso(t)], ...STREAM_COLUMNS[stream].map((c, j) => [c, values[stream][j]])]));
	}
	return { total, columns, rows };
}

export function fixtureStreamPoints(
	symbol: string,
	stream: DataStream,
	query: { timeframe?: string; start?: string | null; end?: string | null; max_points?: number } = {},
	now: string | number | Date = FIXTURE_NOW,
): StreamPointsResponse | null {
	const candidates = fixtureCatalogRows(now).filter((r) => r.symbol === symbol && r.stream === stream && r.venue === 'canonical');
	const row = candidates.find((r) => r.timeframe === query.timeframe) ?? candidates[0];
	if (!row || !row.last_ts || !row.first_ts) return null;
	const maxPoints = query.max_points ?? 1500;
	const end = Math.min(ms(row.last_ts), query.end ? ms(query.end) : ms(row.last_ts));
	const start = Math.max(ms(row.first_ts), query.start ? ms(query.start) : ms(row.first_ts));
	const step = Math.max(tfSeconds(row.timeframe) * 1000, Math.ceil((end - start) / maxPoints));
	const rows = fixtureRows({ symbol, timeframe: row.timeframe, stream }, { start: iso(start), end: iso(end), limit: 1 }, now);
	const points: StreamPointsResponse['points'] = [];
	for (let t = start; t <= end && points.length < maxPoints; t += step) {
		const one = fixtureRows({ symbol, timeframe: row.timeframe, stream }, { start: iso(t), end: iso(t), limit: 1 }, now).rows[0];
		if (one) points.push(one);
	}
	return { symbol, stream, timeframe: row.timeframe, columns: rows.columns, resolution: step === tfSeconds(row.timeframe) * 1000 ? 'raw' : `${Math.round(step / 3_600_000)}h`, raw: step === tfSeconds(row.timeframe) * 1000, points };
}

// ---------------------------------------------------------------- identity, acquisition, import, delete

const REGISTRY_ONLY = ['QNT-USDT', 'HBAR-USDT', 'LTC-USDT', 'TAO-USDT', 'ONDO-USDT', 'PUMP-USDT', 'NVDA-USDT', 'BZ-USDT'];

export function fixtureIdentityResolve(query: string, now: string | number | Date = FIXTURE_NOW): IdentityResolveResponse {
	const rows = fixtureCatalogRows(now);
	const cleaned = query.trim().toUpperCase().replace(/[/_ :]/g, '-');
	const base = cleaned.replace(/-?(USDT|USDC|USD|BTC)$/, '').replace(/(USDT|USDC|USD)$/, '').replace(/-$/, '');
	if (!cleaned) return { query, candidates: [] };
	const symbols = [...new Set([...rows.map((r) => r.symbol), ...REGISTRY_ONLY])]
		.filter((s) => s === cleaned || s.replace(/-/g, '') === cleaned.replace(/-/g, '') || s.split('-')[0] === base || s.startsWith(cleaned))
		.sort((a, b) => (a.endsWith('-USDT') ? 0 : 1) - (b.endsWith('-USDT') ? 0 : 1) || a.localeCompare(b))
		.slice(0, 8);
	const candidates: SymbolCandidate[] = symbols.map((symbol) => {
		const mine = rows.filter((r) => r.symbol === symbol && r.rows > 0);
		const [b, quote = ''] = symbol.split('-');
		const perp = mine.some((r) => r.market === 'perp') || REGISTRY_ONLY.includes(symbol) || (quote === 'USDT' && !mine.length);
		return {
			symbol,
			display_symbol: display(symbol),
			base: b,
			quote,
			asset_class: assetClass(symbol),
			venues: [
				{ venue: 'canonical', market: perp ? 'perp' : 'spot', listed: !DELISTED[symbol], history_start: mine.find((r) => r.stream === 'ohlcv')?.first_ts ?? null },
				...(['BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'AVAX-USDT', 'LINK-USDT', 'HYPE-USDT', 'XRP-USDT'].includes(symbol) ? [{ venue: 'hyperliquid:perp', market: 'perp', listed: true, history_start: '2023-05-12T00:00:00Z' }] : []),
			],
			stored: mine.map((r) => ({ timeframe: r.timeframe, venue: r.venue, stream: r.stream, rows: r.rows, last_ts: r.last_ts })),
			delisted: !!DELISTED[symbol],
			aliases: [b, `${b}${quote}`, `${b}/${quote}`, ...(perp ? [`${b}/${quote}:${quote}`] : [])],
		};
	});
	return { query, candidates };
}

export function fixtureTargets(symbol: string, now: string | number | Date = FIXTURE_NOW): VenueTargetsResponse {
	const resolved = fixtureIdentityResolve(symbol, now).candidates[0];
	const sym = resolved?.symbol ?? symbol.toUpperCase().replace('/', '-');
	const perp = resolved ? resolved.venues[0].market === 'perp' : true;
	const onHl = !!resolved?.venues.some((v) => v.venue === 'hyperliquid:perp');
	const crypto = (resolved?.asset_class ?? 'crypto') === 'crypto';
	const targets: VenueTarget[] = [
		{ venue: 'canonical', exchange: perp ? 'binanceusdm' : 'binance', market: perp ? 'perp' : 'spot', listed: !resolved?.delisted, canonical: true, destination: 'canonical',
			note: 'The research series: backtests, the gauntlet and paper trading read it.' },
		{ venue: 'binance:spot', exchange: 'binance', market: 'spot', listed: crypto, canonical: false, destination: 'venue',
			note: perp ? 'Spot candles, stored separately because this pair has a USD-M perp.' : 'Used as the research series for bases without a perp.' },
		{ venue: 'hyperliquid:perp', exchange: 'hyperliquid', market: 'perp', listed: onHl, canonical: false, destination: 'venue',
			note: 'The execution venue. Stored separately for divergence checks and venue-aware backtests.' },
		{ venue: 'okx:perp', exchange: 'okx', market: 'perp', listed: crypto, canonical: false, destination: 'venue', note: 'Stored separately; never mixed into the research series.' },
		{ venue: 'bybit:perp', exchange: 'bybit', market: 'perp', listed: crypto, canonical: false, destination: 'venue', note: 'Stored separately; never mixed into the research series.' },
		{ venue: 'kraken:spot', exchange: 'kraken', market: 'spot', listed: crypto && ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'DOGE', 'LINK', 'AVAX'].includes(sym.split('-')[0]), canonical: false, destination: 'venue',
			note: 'Full history is rebuilt from trades: slow (hours for 1m).' },
	];
	return { symbol: sym, display_symbol: display(sym), targets };
}

const EXCHANGE_SECONDS_PER_REQUEST: Record<string, number> = { canonical: 0.3, 'binance:spot': 0.3, 'hyperliquid:perp': 0.5, 'okx:perp': 0.4, 'bybit:perp': 0.4, 'kraken:spot': 3 };

export function fixtureEstimate(items: DownloadRequestItem[], now: string | number | Date = FIXTURE_NOW): DownloadEstimateResponse {
	const t = toMs(now);
	const rows = fixtureCatalogRows(now);
	const disk = fixtureStorage(now).disk.free_bytes;
	const estimates: DownloadEstimate[] = items.map((item) => {
		const targets = fixtureTargets(item.symbol, now).targets;
		const target = targets.find((x) => x.venue === item.venue);
		const existing = rows.find((r) => r.symbol === item.symbol && r.timeframe === item.timeframe && r.stream === 'ohlcv' && r.venue === item.venue && r.rows > 0);
		const listedFrom = ms(fixtureIdentityResolve(item.symbol, now).candidates[0]?.venues[0].history_start ?? '2020-01-01T00:00:00Z');
		const from = item.history.mode === 'all' ? listedFrom : item.history.mode === 'days' ? t - item.history.days * DAY : ms(item.history.start);
		const to = item.history.mode === 'range' ? ms(item.history.end) : t;
		const bars = Math.max(0, Math.floor((to - from) / (tfSeconds(item.timeframe) * 1000)));
		const covered = existing?.first_ts && existing.last_ts ? Math.max(0, Math.min(to, ms(existing.last_ts)) - Math.max(from, ms(existing.first_ts))) / (tfSeconds(item.timeframe) * 1000) : 0;
		const newBars = Math.max(0, Math.round(bars - covered));
		const requests = Math.ceil(newBars / 1000) + (item.streams?.length ?? 0) * 3;
		const warnings: string[] = [];
		if (item.timeframe === '1m' && newBars > 1_000_000) warnings.push(`1m history is large: about ${Math.round((newBars * 23) / MB)} MB and ${(newBars / 1e6).toFixed(1)} M bars.`);
		if (item.venue === 'kraken:spot') warnings.push('Kraken rebuilds history from trades; expect this to take hours.');
		const blocked = !target ? `${item.venue} is not a download venue for ${item.symbol}` : !target.listed ? `${target.exchange} does not list ${display(item.symbol)}` : null;
		return {
			item,
			destination: target?.destination ?? 'venue',
			existing_rows: existing?.rows ?? 0,
			existing_first: existing?.first_ts ?? null,
			existing_last: existing?.last_ts ?? null,
			new_bars_estimate: blocked ? 0 : newBars,
			bytes_estimate: blocked ? 0 : newBars * 23 + (item.streams?.length ?? 0) * Math.round(newBars / 8) * 18,
			seconds_estimate: blocked ? 0 : Math.round(requests * (EXCHANGE_SECONDS_PER_REQUEST[item.venue] ?? 0.4)),
			requests_estimate: blocked ? 0 : requests,
			warnings,
			blocked,
		};
	});
	const totalBytes = estimates.reduce((s, e) => s + e.bytes_estimate, 0);
	return {
		estimates,
		total_bytes: totalBytes,
		total_seconds: estimates.reduce((s, e) => s + e.seconds_estimate, 0),
		disk_free_bytes: disk,
		warnings: totalBytes > disk * 0.5 ? ['This would use more than half of the free disk space.'] : [],
	};
}

export function fixtureImportPreview(filename = 'XAUUSD_1h_2025.csv', target?: { symbol?: string; timeframe?: string }, now: string | number | Date = FIXTURE_NOW): ImportPreview {
	const start = ms('2025-01-02T00:00:00Z');
	const hour = 3_600_000;
	const sample = Array.from({ length: 5 }, (_, i) => {
		const t = start + i * hour;
		const p = priceAt('XAU-USD', t);
		const d = new Date(t);
		return {
			Date: `${d.getUTCFullYear()}.${String(d.getUTCMonth() + 1).padStart(2, '0')}.${String(d.getUTCDate()).padStart(2, '0')} ${String(d.getUTCHours()).padStart(2, '0')}:00`,
			Open: +p.toFixed(2), High: +(p * 1.002).toFixed(2), Low: +(p * 0.998).toFixed(2), Close: +priceAt('XAU-USD', t + hour).toFixed(2), Volume: 1200 + i * 37,
		};
	});
	const symbol = (target?.symbol ?? '').toUpperCase().replace('/', '-');
	const existing = symbol ? findRow({ symbol, timeframe: target?.timeframe ?? '1h' }, now) : undefined;
	return {
		filename,
		rows: 6_214,
		columns: ['Date', 'Open', 'High', 'Low', 'Close', 'Volume'],
		mapping: { timestamp: 'Date', open: 'Open', high: 'High', low: 'Low', close: 'Close', volume: 'Volume' },
		required_ok: true,
		sample,
		parsed_sample: sample.map((row, i) => ({ t: iso(start + i * hour), o: row.Open, h: row.High, l: row.Low, c: row.Close, v: row.Volume })),
		first_ts: iso(start),
		last_ts: '2025-12-31T21:00:00Z',
		inferred_timeframe: '1h',
		timeframe_confidence: 0.962,
		misaligned_rows: 0,
		invalid_rows: 2,
		target: symbol
			? {
				symbol,
				timeframe: target?.timeframe ?? '1h',
				exists: !!existing,
				destination: existing?.venue === 'canonical' ? 'canonical' : 'venue',
				existing_rows: existing?.rows ?? 0,
				existing_source: existing?.source ?? null,
				overlap: existing
					? { new_bars: 214, identical: 5_812, conflicting: 186, conflict_examples: [
						{ t: '2025-03-10T13:00:00Z', stored_close: 2_914.2, file_close: 2_916.85 },
						{ t: '2025-06-02T08:00:00Z', stored_close: 3_352.1, file_close: 3_350.4 },
					] }
					: { new_bars: 6_212, identical: 0, conflicting: 0, conflict_examples: [] },
			}
			: null,
		errors: [],
		warnings: ['2 rows have a non-numeric close and will be skipped.', 'Timestamps carry no time zone; they are read as UTC unless you pick another.', 'Weekend hours are absent (market closed): 3.8% of consecutive rows are more than 1 h apart.'],
	};
}

export function fixtureDeleteCheck(key: SeriesKey, now: string | number | Date = FIXTURE_NOW): DeleteCheck {
	const row = findRow(key, now);
	const consumers = CONSUMERS.filter((c) => row && c.series.includes(row.id)).map((c) => c.ref);
	const tier = row?.sla.tier ?? 'idle';
	const blocking = ['live', 'paper', 'pipeline'].includes(tier) && consumers.length > 0;
	const willRebootstrap = ['live', 'paper', 'pipeline', 'universe'].includes(tier);
	const warnings: string[] = [];
	if (blocking) warnings.push(`${consumers.length} ${tier} consumer${consumers.length === 1 ? '' : 's'} read this series; they will fail their data checks until it is downloaded again.`);
	if (willRebootstrap) warnings.push('The collector will download this series again, because it is in the research universe or a strategy needs it.');
	return {
		series: key,
		exists: !!row && row.rows > 0,
		bytes: row?.size_bytes ?? 0,
		rows: row?.rows ?? 0,
		consumers,
		blocking,
		will_rebootstrap: willRebootstrap,
		warnings,
		confirm_phrase: key.stream === 'ohlcv' ? `delete ${key.symbol} ${key.timeframe}` : `delete ${key.symbol} ${key.stream} ${key.timeframe}`,
	};
}
