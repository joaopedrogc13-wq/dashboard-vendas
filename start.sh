#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  start.sh — Inicia API + Túnel Cloudflare e atualiza GitHub
# ─────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Defina GITHUB_TOKEN no ambiente antes de rodar (ex: export GITHUB_TOKEN=seu_token)
TOKEN="${GITHUB_TOKEN:?'Erro: variável GITHUB_TOKEN não definida. Execute: export GITHUB_TOKEN=seu_token'}"
REPO="joaopedrogc13-wq/dashboard-vendas"
HTML="$SCRIPT_DIR/dashboard_vendas.html"
LOG="$SCRIPT_DIR/tunnel.log"

echo "[$(date '+%H:%M:%S')] Iniciando API de Vendas..."
pkill -f "api_vendas.py" 2>/dev/null || true
sleep 1
python3 "$SCRIPT_DIR/api_vendas.py" &
API_PID=$!
echo "[$(date '+%H:%M:%S')] API iniciada (PID $API_PID)"

echo "[$(date '+%H:%M:%S')] Iniciando túnel Cloudflare..."
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 1
cloudflared tunnel --url http://localhost:8742 --no-autoupdate 2>"$LOG" &
CF_PID=$!
echo "[$(date '+%H:%M:%S')] Aguardando URL do túnel..."

# Aguarda até 30s pela URL
TUNNEL_URL=""
for i in $(seq 1 30); do
    sleep 1
    TUNNEL_URL=$(grep -o 'https://[a-zA-Z0-9\-]*\.trycloudflare\.com' "$LOG" 2>/dev/null | head -1)
    if [ -n "$TUNNEL_URL" ]; then break; fi
done

if [ -z "$TUNNEL_URL" ]; then
    echo "[ERRO] Não foi possível obter URL do túnel. Verifique $LOG"
    exit 1
fi

echo "[$(date '+%H:%M:%S')] Túnel ativo: $TUNNEL_URL"

# Atualiza URL no HTML
echo "[$(date '+%H:%M:%S')] Atualizando dashboard_vendas.html..."
sed -i '' "s|https://[a-zA-Z0-9\-]*\.trycloudflare\.com|$TUNNEL_URL|g" "$HTML"

# Commit e push para GitHub
echo "[$(date '+%H:%M:%S')] Publicando nova URL no GitHub..."
cd "$SCRIPT_DIR"
git add dashboard_vendas.html
git commit -m "chore: atualiza URL do túnel Cloudflare ($TUNNEL_URL)" \
    --author="G4 OS <g4os@g4business.com>" 2>/dev/null || echo "(sem mudanças no git)"
git push "https://${TOKEN}@github.com/${REPO}.git" main 2>/dev/null

echo ""
echo "✅ Tudo pronto!"
echo "   API local  : http://localhost:8742"
echo "   Túnel       : $TUNNEL_URL"
echo "   Dashboard   : https://joaopedrogc13-wq.github.io/dashboard-vendas/dashboard_vendas.html"
echo ""
echo "   PIDs: API=$API_PID  Túnel=$CF_PID"
echo "   Para parar: pkill -f api_vendas.py && pkill -f 'cloudflared tunnel'"
