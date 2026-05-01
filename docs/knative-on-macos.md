# Knative を macOS で動かす（local 完結）

このリポジトリの function（buildings-cng）を Cloudflare Tunnel や 専用 Linux ホスト（z-t）抜きで、 **macOS の Docker Desktop だけ** で動かす手順。 「k8s も Knative も触ったことが無い人」 を想定する。 動作確認用としても使えるし、 自分の dataset で同等の study を始める時の最小ベースラインとしても使える。

## 前提

- macOS（Apple Silicon / Intel どちらでも）
- [Docker Desktop](https://www.docker.com/products/docker-desktop/)（メモリ 4 GB 以上を割り当て）
- [Homebrew](https://brew.sh/)

メモリ目安: kind の k8s ノード + Knative の `activator` / `autoscaler` / `controller` / `webhook` + Kourier + ksvc 自身を全部立てるので、 Docker Desktop に 6-8 GB 振るのが快適。

## 最短: `kn quickstart` で 5 分セットアップ

Knative 公式の CLI が、 **kind（Kubernetes in Docker）+ Knative Serving + Kourier + ingress 設定 + sslip.io domain** を 1 コマンドで作ってくれる。

```bash
brew install knative/client/kn
brew install knative/client/kn-quickstart

kn quickstart kind
```

完了すると以下が立っている:

- `knative` という名前の kind クラスタ（k8s ノード 1 個）
- `knative-serving` namespace に Serving 一式
- `kourier-system` namespace に Kourier
- `config-domain` configmap が `127.0.0.1.sslip.io` に設定済み
- ksvc URL は `http://<svc>.<ns>.127.0.0.1.sslip.io:31080/` 形式で叩ける

確認:

```bash
kubectl get pods -n knative-serving
kubectl get pods -n kourier-system
```

すべて `Running` になっていれば OK。

## helloworld-go で経路確認

ネットワーク経路がちゃんと立っているか、 公式 sample を 1 つ流す:

```yaml
# /tmp/helloworld.yaml
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: helloworld-go
  namespace: default
spec:
  template:
    spec:
      containers:
        - image: gcr.io/knative-samples/helloworld-go
          env:
            - name: TARGET
              value: "Hello DWG7"
```

```bash
kubectl apply -f /tmp/helloworld.yaml
kubectl get ksvc helloworld-go
```

URL は次のような形式:

```
http://helloworld-go.default.127.0.0.1.sslip.io:31080/
```

curl で叩く:

```bash
curl http://helloworld-go.default.127.0.0.1.sslip.io:31080/
# => Hello Hello DWG7!
```

数十秒アイドルにすると pod が 0 に落ちる（scale-to-zero）。 もう一度 curl を打つと cold start で 1-3 秒待たされて、 同じ応答が返る。 これが Knative の核心の挙動。

## buildings-cng を local で動かす

このリポジトリの function 部分を kind クラスタ上に乗せる手順。

### 1. image を build

```bash
docker build -t buildings-cng:0.1.0 -f docker/Dockerfile .
```

### 2. kind クラスタに image を load

`kn quickstart kind` で立てた クラスタの名前は `knative`:

```bash
kind load docker-image buildings-cng:0.1.0 --name knative
```

これで `imagePullPolicy: IfNotPresent` の ksvc が image を見つけられるようになる。

### 3. namespace と ksvc を apply

リポジトリの `k8s/ksvc.yaml` は `namespace: knative-pool` を想定しているので、 local でもその ns を作る:

```bash
kubectl create ns knative-pool
kubectl apply -f k8s/ksvc.yaml
```

### 4. tag-resolution を skip させる

`buildings-cng:0.1.0` のような registry-less tag は、 Knative が起動時に Docker Hub に digest 解決しに行って失敗する。 z-t と同じ patch を当てる:

```bash
kubectl -n knative-serving patch cm config-deployment --type merge \
  -p '{"data":{"registries-skipping-tag-resolving":"docker.io,index.docker.io,kind.local"}}'
kubectl -n knative-pool delete ksvc buildings-cng
kubectl apply -f k8s/ksvc.yaml
```

### 5. ksvc Ready を待つ

```bash
kubectl get ksvc buildings-cng -n knative-pool -w
```

`READY=True` になれば URL が振られている:

```
http://buildings-cng.knative-pool.127.0.0.1.sslip.io:31080
```

### 6. health check

```bash
curl http://buildings-cng.knative-pool.127.0.0.1.sslip.io:31080/health
# => {"ok":true,"release":"2026-04-15.0","stac":{...}}
```

### 7. viewer を local で配信して叩く

```bash
uv run python -m http.server --directory docs 8000
```

ブラウザで:

```
http://localhost:8000/?server=http://buildings-cng.knative-pool.127.0.0.1.sslip.io:31080
```

これで Cloudflare Tunnel も外部 Linux ホストも一切無しで、 動的ベクトルタイル生成が体感できる。

## 後片付け

```bash
kn quickstart kind --uninstall
# または
kind delete cluster --name knative
```

Docker Desktop の Kubernetes 組み込み（Settings → Kubernetes → Enable）経由でやる選択肢もあるが、 quickstart より手間が増える（Knative Serving CRDs / core / Kourier をすべて手で apply する必要）。 学習目的なら kind 経由が圧倒的に楽。

## トラブルシューティング

| 症状 | 原因 / 対処 |
| --- | --- |
| `ksvc` が `RevisionMissing / 401 Unauthorized` で落ちる | `registries-skipping-tag-resolving` patch（手順 4）を忘れている。 当ててから再 apply |
| ksvc が `Ready=Unknown` のまま | `kubectl get pods -n knative-pool` で pod の状態を見る。 image pull が失敗していたら `kind load docker-image` をやり直す |
| curl でタイムアウト | Docker Desktop のメモリ不足で activator / Kourier が swap している。 メモリ割当を 6-8 GB に増やす |
| ports 31080 が他で使われている | `kubectl get svc -n kourier-system kourier -o yaml` で nodePort を確認。 Docker Desktop の port forward が衝突していないか確認 |

## 関連

- [Knative quickstart 公式](https://knative.dev/docs/getting-started/quickstart-install/)
- [kind 公式](https://kind.sigs.k8s.io/)
- 本リポジトリの z-t 向け手順は README の「function (z-t Knative)」 セクションを参照。 macOS local と z-t bare-metal kubeadm では `image を ctr で import するか kind で load するか` が違うだけで、 ksvc YAML / patch コマンドは同じものが使える
