# Freehand Georeferencer 実装計画

既存の **Freehand Vector Georeferencer**（ベクタ専用）は残したまま、ベクタ／ラスタ両対応の
新プラグイン **Freehand Georeferencer** を別プラグインとして新規作成する。

## 設計方針

「共通基底クラス + データ型別バックエンド」を1プラグイン内に持つ。
数学・GCP管理・統計・残差表示・CSV入出力・CRS警告・dockの骨格はすべて共有し、
データ型固有部（プレビュー描画・スナップ・出力）だけをサブクラスで差し替える。

```
GeorefSessionBase  (georef_session_base.py)
  GCP/行列/recompute/統計/マーカー/残差/CSV/レイヤ可視制御/出力先スナップ
   ├ VectorGeorefSession (georef_vector_session.py)
   │    rubber band プレビュー / 頂点アンカー / scope(single,selected,all)
   │    geometry 変換出力（新規memory / 追記 / 上書き編集）
   └ RasterGeorefSession (georef_raster_session.py)
        画像オーバーレイ(QgsMapCanvasItem)プレビュー / アンカー無し(自由点)
        geotransform 書き換え出力（VRT、リサンプリング無し・無劣化）
```

## 重要な技術的前提

- 変換は Helmert / Affine のみ。**Affine = GDAL ジオトランスフォームそのもの**なので、
  ラスタ出力はピクセル再標本化が不要。元ラスタを参照する VRT に新しい geotransform を
  書くだけで、無劣化・瞬時に適用できる。
- ラスタには頂点が無いため「ノードをつかむ」スナップは成立しない。ソース指定は
  自由点クリック（＋他ベクタレイヤ頂点へのスナップは流用可）。移動先スナップは流用。
- プレビューは、セッション開始時に元ラスタを QImage へ一度レンダリングし、ドラッグ中は
  その画像に `QTransform`（quadToQuad）を掛けて canvas にオーバーレイ描画する。

## geotransform 合成式

元 GT: `X = g0 + col*g1 + row*g2`, `Y = g3 + col*g4 + row*g5`
変換 M(2x3): `world' = L·world + t`（L=[[M00,M01],[M10,M11]], t=[M02,M12]）

```
ng0 = M00*g0 + M01*g3 + M02
ng1 = M00*g1 + M01*g4
ng2 = M00*g2 + M01*g5
ng3 = M10*g0 + M11*g3 + M12
ng4 = M10*g1 + M11*g4
ng5 = M10*g2 + M11*g5
```

## 実装ステップ

1. [x] 新プラグイン雛形（`__init__.py` / `freehand_georeferencer.py` / `metadata.txt` /
       icon・LICENSE・transform.py を流用）
2. [x] `georef_session_base.py`：共通基底を抽出
3. [x] `georef_vector_session.py`：`VectorGeorefSession(GeorefSessionBase)`
4. [x] `georef_raster_session.py`：`RasterGeorefSession(GeorefSessionBase)`
       - 元ラスタ→QImage レンダリング
       - `RasterPreviewItem(QgsMapCanvasItem)` で affine オーバーレイ
       - `apply()` で GDAL VRT を生成しプロジェクトへ追加
5. [x] `georef_maptool.py`：`snap_source` をセッションへ委譲（ラスタは None→自由点）
6. [x] `georef_dockwidget.py`：
       - レイヤフィルタを Vector+Raster
       - レイヤ型で scope / apply-as の選択肢を出し分け
       - 型に応じて Vector/Raster セッションを生成
7. [x] QGIS プラグインフォルダへ配置し `py_compile` で構文チェック

## 既知の制約・要テスト項目（QGIS実機）

- ラスタ出力はファイル系データソース前提（`layer.source()` が GDAL で開けること）。
  WMS等のサービス系は対象外。
- VRT は元ファイルを絶対パス参照する。元ファイルを移動すると壊れる。
- `RasterPreviewItem` の座標変換（`toCanvasCoordinates` と item 原点）は QgsMapCanvasItem の
  実機挙動に依存。表示位置ズレが出た場合はここを調整。
- Affine に回転/スキューが入った VRT geotransform を QGIS が正しく表示するか要確認。
- 同一CRS運用（レイヤCRS=プロジェクトCRS）はベクタ版と同じ前提。
