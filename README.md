# study-cng-overture-buildings-tile

> **A Cloud Native Geospatial study: dynamic vector tiles served on the fly from Overture Maps GeoParquet via DuckDB Spatial + STAC + Knative.** No tile pre-build, no PMTiles, no static MBTiles. The tile bytes are synthesized per request from cloud-native primitives.

| | |
| --- | --- |
| **viewer (static)** | https://yuiseki.github.io/study-cng-overture-buildings-tile/ |
| **function (serverless)** | https://buildings-cng.yuiseki.com/tiles/{z}/{x}/{y}.mvt |
| **example URL** | https://buildings-cng.yuiseki.com/tiles/14/4823/6155.mvt?filter_by_height=20m |

## Screenshots

[![Manhattan first view: all Overture buildings, height-colored 3D extrusion](https://i.gyazo.com/e1570b0ec5671a26768292974384288f.jpg)](https://gyazo.com/e1570b0ec5671a26768292974384288f)

<sub>filter なし: Manhattan の Overture Buildings 全件、高さで色分けした 3D extrusion</sub>

[![Manhattan filtered to height >= 100m via filter_by_height=100m URL parameter](https://i.gyazo.com/940a02c21748b16746180bf0cff94999.jpg)](https://gyazo.com/940a02c21748b16746180bf0cff94999)

<sub>`filter_by_height=100m`: クエリパラメータで 100m 以上の超高層ビルだけにフィルタ。事前ビルドではなく、サーバ側で SQL が走り直して別の MVT が返る</sub>

## なぜ作ったか

Vector tile を配信する OSS は「事前にビルドして静的に配る」系（[planetiler], [tippecanoe], [PMTiles]）が主流で、**動的に bbox + クエリパラメータでフィルタしてオンザフライで MVT を吐く**ものは事実上空白に近い。

ラスタの世界には [TiTiler] が「STAC + COG をオンザフライで配る動的サーバ」として完成度が高いポジションを占めているが、それの **ベクター版** に相当するものが見当たらない。

このリポジトリは「TiTiler のベクター版が成り立つか」を、[Cloud Native Geospatial][cng] のベクター系プリミティブだけで組み上げて確かめる study です。具体的には:

- データ源は [**Overture Maps**][overture] の `theme=buildings` GeoParquet（AWS S3 公開バケット、 276 GB / 512 file）
- 場所の絞り込みは [**Overture STAC catalog**][overture-stac] を使った client-side spatial index
- 実際の partial read + フィルタは **DuckDB Spatial + httpfs** が S3 に対して直接実施
- レンダリングは [**MapLibre GL JS**][maplibre] の `fill-extrusion` で 3D
- サーバ側は **Knative Serving** ksvc としてデプロイ、scale-to-zero で使われていないときは pod 0、 リクエストで起動

[planetiler]: https://github.com/onthegomap/planetiler
[tippecanoe]: https://github.com/felt/tippecanoe
[PMTiles]: https://github.com/protomaps/PMTiles
[TiTiler]: https://github.com/developmentseed/titiler
[cng]: https://cloudnativegeo.org/
[overture]: https://overturemaps.org/
[overture-stac]: https://docs.overturemaps.org/blog/2026/02/11/stac/
[maplibre]: https://maplibre.org/

## できること

- ブラウザでパン・ズームすると、その bbox に該当する Overture Buildings の polygon が **動的に** 取得される（事前のタイルセット build なし）
- スライダーを動かすと **`filter_by_height=10m`** のようなクエリパラメータが毎回切り替わり、サーバ側で違う SQL が走り、即座に結果が変わる
- 同じインフラで `filter_by_class`, `filter_by_floors` のようなフィルタの追加は SQL 1 行と URL パラメータ 1 個の追加で実現できる
- viewer は GitHub Pages、function は Knative ksvc というフロント／バックのクリーンな分離

## アーキテクチャ

```
┌──────────────────────────────────────────┐         ┌─────────────────────────────────┐
│ frontend (static)                        │  HTTPS  │ function (dynamic)              │
│ yuiseki.github.io/                       │ ──────► │ buildings-cng.yuiseki.com       │
│ study-cng-overture-buildings-tile/       │         │ (Knative ksvc on z-t k8s)       │
│   docs/index.html  (MapLibre GL JS)      │         │   FastAPI (uvicorn)             │
│   docs/style.json  (Esri World Imagery)  │         │     ↓                           │
└──────────────────────────────────────────┘         │   STAC spatial index            │
                                                     │   (in-memory, built at startup) │
                                                     │     ↓                           │
                                                     │   DuckDB Spatial + httpfs       │
                                                     │     ↓                           │
                                                     │   s3://overturemaps-us-west-2/  │
                                                     │     release/.../buildings/      │
                                                     └─────────────────────────────────┘
```

### 1 リクエストの流れ

1. ブラウザが `https://buildings-cng.yuiseki.com/tiles/14/4823/6155.mvt?filter_by_height=20m` に GET
2. Cloudflare Tunnel が z-t の Knative Kourier 入口（NodePort）に転送
3. Knative が ksvc（pod 数 0 ならここで cold start、 30 秒以内）にルーティング
4. FastAPI ハンドラが `(z, x, y)` から bbox を計算
5. **STAC index** から bbox に該当する Parquet ファイル（512 個中の 1〜数個）の `s3://` URL を取得
6. **DuckDB Spatial** がその数個のファイルだけを `read_parquet([file1, file2, ...])` で開き、 row group prune + bbox WHERE 句で絞り込み
7. 結果を WKB で取得、 [`mapbox-vector-tile`][mvt] で MVT bytes にエンコード
8. レスポンス

[mvt]: https://github.com/tilezen/mapbox-vector-tile

### Cold start の見積もり

| ステージ | 時間 | 内訳 |
| --- | ---: | --- |
| コンテナ起動 | ~5 秒 | Knative pod スケジューリング |
| DuckDB extension load | ~3 秒 | `spatial` + `httpfs` の初回 install |
| STAC index build | ~7 秒 | `collection.json` + 512 個の `item.json` を並列 fetch |
| 1 タイル目の query | ~10 秒 | 1 ファイル目の Parquet metadata fetch + row group read |
| **合計** | **~25 秒** | これ以降 はキャッシュが効いて 0.5〜2 秒 / タイル |

scale-to-zero された後、 1 個目のリクエストでこれが走る。 2 個目以降は ksvc が pod を保持している間（idle まで 60 秒）はホットで叩ける。

## 動かす

### ローカル開発

```bash
# function サーバ（DuckDB + FastAPI、 port 8006）
uv sync
uv run python -m buildings_cng.server

# viewer（別ターミナル、 port 8000）
uv run python -m http.server --directory docs 8000

# ブラウザ
open 'http://localhost:8000/?server=http://localhost:8006'
```

### viewer を GitHub Pages にデプロイ

リポジトリの **Settings → Pages**:

- Source: `Deploy from a branch`
- Branch: `main` / Folder: `/docs`

これで `https://<owner>.github.io/study-cng-overture-buildings-tile/` に viewer が公開される。 viewer はデフォルトで本番 function（`https://buildings-cng.yuiseki.com`）を叩くので、 function 側を先に立てておくこと。

### function を Knative にデプロイ

事前条件: Knative Serving + Kourier が入った k8s クラスタ（このリポジトリは bare-metal kubeadm + Cloudflare Tunnel の構成で動作確認）。

```bash
# image を build して containerd の k8s.io namespace に import
docker build -t buildings-cng:0.1.0 -f docker/Dockerfile .
docker save buildings-cng:0.1.0 | ctr -n=k8s.io images import -

# ksvc を apply
kubectl apply -f k8s/ksvc.yaml

# 確認
kubectl get ksvc buildings-cng -n knative-pool
```

macOS の Docker Desktop だけで local 完結に動かす手順は [docs/knative-on-macos.md](./docs/knative-on-macos.md) を参照。 Cloudflare Tunnel や外部 Linux ホストは要らず、 `kn quickstart kind` から始めて 5 分で立つ。 k8s / Knative を初めて触る人向けの最小経路として用意してある。

ksvc URL は domain-template に従って `<name>.<base-domain>` で自動生成され、このプロジェクトでは `buildings-cng.yuiseki.com` になる。

## 使う側のクエリパラメータ

```
GET /tiles/{z}/{x}/{y}.mvt
```

| パラメータ | 形式 | 説明 |
| --- | --- | --- |
| `filter_by_height` | `"10m"` 等 human-readable | 最小ビル高さ（メートル）。省略で全件 |
| `limit` | int | 1 タイルあたりの最大 feature 数（デフォルト 5000）|

viewer 側の URL params:

| param | 説明 |
| --- | --- |
| `server` | function の URL（デフォルト `https://buildings-cng.yuiseki.com`、 ローカル開発時は `http://localhost:8006`） |
| `city` | プリセットビュー（`manhattan` / `tokyo`） |
| `height` | 初期 `filter_by_height` 値（数値、メートル） |

## 既知の制約

- **2 タイル目以降のレイテンシは ~0.5 秒**で安定するが、 cold start の ~25 秒は serverless として体感的に遅い。 STAC index を起動時に sync fetch しているのが主因
- **Overture release は実装でハードコード**（`OVERTURE_RELEASE = "2026-04-15.0"`）。 release 切替はコード変更 + 再デプロイが必要
- **タイル / クエリ結果のキャッシュ層は持たない**。 同じ bbox + filter のリクエストは毎回 DuckDB が走る
- **対応 theme は `buildings` のみ**。 places / transportation / divisions 等は未対応

## 関連 OSS / 着想元

- [TiTiler]: ラスタ用の動的タイルサーバ。 このプロジェクトの直接の手本
- [Overture Maps][overture]: データ源。 STAC catalog の整備によってこの種の動的サービスが現実的になった
- [DuckDB Spatial](https://duckdb.org/docs/extensions/spatial): S3 GeoParquet に対する partial read + 空間 SQL のキーパーツ
- [Knative Serving](https://knative.dev/): scale-to-zero の serverless 実行
- [Cloud Native Geospatial Forum][cng]: このカテゴリそのものを定義した場

## 設計ノート

### なぜ static tile build しないのか

事前ビルドした PMTiles / MBTiles を CDN で配るのが速度・コスト上は最適解で、 production ではそれが正解になることが多い。 ただし:

- **データ更新の即時反映ができない**（再 build が必要）
- **クエリパラメータでの動的フィルタができない**（`filter_by_height` のような軸が増えるたびにタイルセットが乗算で増える）
- **ビルドのインフラが要る**（数百 GB の Overture を全 zoom レベル分処理するのは小さくないコスト）

「クエリパラメータで動的に変わる結果を、 タイルとして配る」 という形は事前ビルドでは不可能なので、 そこを on-the-fly 計算で埋める実験の意義がある。

### なぜ STAC index を自前で持つのか

DuckDB の `read_parquet('s3://.../*')` で wildcard を渡すと、 S3 の 512 ファイル全部の Parquet footer を fetch しに行き、 cold start で 1〜2 分かかる。 これは Overture が ID 順 partition で **ファイル名から geographic 位置が決まらない** ため。

Overture が公式に提供している **STAC catalog** には各ファイルの bbox が個別に載っているので、 起動時に 1 度だけそれを取って in-memory index を作れば、 リクエスト時には bbox に該当する数ファイルだけを `read_parquet` に渡せる。 これが本プロジェクトのキーアイデアで、 cold start を 1〜2 分から 25 秒に縮めている。

### なぜ Knative なのか

「使われていないときは pod 0、 リクエストが来たら起動する」 serverless の挙動が、 動的タイルサーバとぴったり合う。 タイルが叩かれない時期は GPU/CPU を解放できる。 運用面でも Cloudflare Tunnel との組み合わせで public URL を 1 行（wildcard）で生やせる。

## 学び

### DuckDB-Spatial は Jupyter から外に出すと壊れる

実装中に踏んだ最大の罠。 FastAPI + uvicorn の thread pool から **同じ DuckDB connection に対して concurrent に query を投げると、 _duckdb.cpython-*.so の中で SIGSEGV** する。 ブラウザがタイルを並列 fetch するたびに即落ちる。

公式 doc は core を thread-safe と謳う（[Concurrency in DuckDB](https://duckdb.org/docs/connect/concurrency)）が、**extension の thread safety は保証外**。 今回踏んだ経路は:

- **`spatial`**: 内部で GEOS / proj の C++ オブジェクトを使う。 libgeos は `GEOSContext_createHandle` で thread-local handle を取るのが原則で、 グローバル state を share すると race
- **`httpfs`**: libcurl の easy / multi handle を使う。 一般に share せず thread-per-handle が安全

つまり **「DuckDB core 単体なら concurrent OK、 spatial + httpfs を同 connection で concurrent に叩くと壊れる」**。 Jupyter notebook では single-thread しか使わないので顕在化しない。

#### 対処の選択肢

| 方法 | 実装 | 並列度 | 適性 |
| --- | --- | --- | --- |
| `threading.Lock()` で serialize | 数行 | 0（直列） | PoC / 低トラフィック |
| `con.cursor()` per request | 中 | 中 | 単 process 内で多少並列に |
| `duckdb.connect()` per thread | 中 | 高 | 単 process でスループット重視 |
| 1 process = 1 connection（複数 pod でスケール） | 設定のみ | 水平 | **k8s / Knative 前提なら筋** |

このリポジトリは PoC 規模なので最も単純な lock を採用している（`src/buildings_cng/duckdb_query.py` の `_query_lock`）。 production スループットを取るなら、 同 process 内で並列化を頑張るより **Knative の autoscale で pod 数を増やす** ほうが k8s の哲学とも合う。

### Overture を on-the-fly に query するなら STAC catalog が前提

DuckDB の `read_parquet('s3://.../*')` で wildcard を渡すと、 S3 の 512 ファイル全部の Parquet footer を fetch しに行く（→ cold start 1〜2 分）。 Overture は ID 順 partition で **ファイル名から geographic 位置が決まらない**ので、 wildcard ではどの file を読めばよいか DuckDB に伝わらない。

Overture が 2026-02 から提供している [**STAC catalog**](https://docs.overturemaps.org/blog/2026/02/11/stac/) には各ファイルの bbox が item として個別に載っている。 起動時に 1 度だけ collection.json + 全 item.json を並列 fetch して in-memory spatial index を作っておけば、 リクエスト時は bbox に該当する数ファイルだけを `read_parquet([...])` に渡せる。 これで cold start が 1〜2 分から **25 秒** に縮む。 オンザフライで Overture を query する系のサービスを作るなら、 この前処理は必須に近い。

## License

実装コードは [MIT License](./LICENSE.md)。 Overture Maps データの利用は [Overture data license](https://docs.overturemaps.org/attribution/) に従うこと（基本は CDLA Permissive 2.0 / ODbL、 theme による）。
