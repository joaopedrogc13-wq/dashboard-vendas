"""
API de dados — Dashboard de Vendas Cabral & Sousa
Vendas  : CSOUSA.K_VENDA (SUM(QTVENDIDA * PRECOUNITCONT), filtros de negócio aplicados)
Custo   : CSOUSA.K_VENDA (CUSTOREAL)
Dimensões: K_VENDA (CODUSUR/CODGERENTE/CODSUPERVISOR), PCUSUARI, PCGERENTE, PCFORNEC
Meta    : CSOUSA.PCMETA (TIPOMETA IN ('F','D'), 1 linha/mês/RCA)
Cache   : TTL=5min por (endpoint, params)
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, oracledb, traceback, time, threading
from urllib.parse import urlparse, parse_qs
from datetime import datetime

DB_CONFIG = {"user": "POWERBI", "password": "PBIGEST10*", "dsn": "10.122.74.100:1521/PROD"}

# ─── CACHE ────────────────────────────────────────────────────────────────────
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # 5 minutos

def cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry['ts']) < CACHE_TTL:
            return entry['data']
    return None

def cache_set(key, data):
    with _cache_lock:
        _cache[key] = {'data': data, 'ts': time.time()}

# ─── DB ───────────────────────────────────────────────────────────────────────
def get_conn():
    return oracledb.connect(**DB_CONFIG)

def query(sql, params=None):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(sql, params or {})
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    conn.close()
    def fmt(v):
        if isinstance(v, datetime): return v.strftime("%Y-%m-%d")
        return v
    return [dict(zip(cols, [fmt(x) for x in r])) for r in rows]

def cached_query(cache_key, sql, params=None):
    result = cache_get(cache_key)
    if result is not None:
        return result
    result = query(sql, params)
    cache_set(cache_key, result)
    return result

# ─── FILTROS DE PERÍODO ───────────────────────────────────────────────────────
def kv_periodo(mes_inicio, mes_fim, alias='k'):
    col = f"{alias}.DTSAIDA"
    if mes_inicio and mes_fim:
        return f"AND {col} >= TO_DATE('{mes_inicio}-01','YYYY-MM-DD') AND {col} < ADD_MONTHS(TO_DATE('{mes_fim}-01','YYYY-MM-DD'),1)"
    elif mes_inicio:
        return f"AND {col} >= TO_DATE('{mes_inicio}-01','YYYY-MM-DD') AND {col} < ADD_MONTHS(TO_DATE('{mes_inicio}-01','YYYY-MM-DD'),1)"
    return f"AND {col} >= TRUNC(SYSDATE,'MM') AND {col} < ADD_MONTHS(TRUNC(SYSDATE,'MM'),1)"

def meta_periodo(mes_inicio, mes_fim):
    if mes_inicio and mes_fim:
        return f"TRUNC(DATA,'MM') >= TO_DATE('{mes_inicio}-01','YYYY-MM-DD') AND TRUNC(DATA,'MM') <= TO_DATE('{mes_fim}-01','YYYY-MM-DD')"
    elif mes_inicio:
        return f"TRUNC(DATA,'MM') = TO_DATE('{mes_inicio}-01','YYYY-MM-DD')"
    return "TRUNC(DATA,'MM') = TRUNC(SYSDATE,'MM')"

# Filtros de negócio para K_VENDA
KV_BASE = """
    AND k.DTCANCELAMENTO IS NULL
    AND k.CODOPER IN ('S','SM','SB')
    AND NVL(k.TIPOMERC,'XX') NOT IN ('PB')
    AND (k.TIPO != 'B' OR k.TIPO IS NULL)
    AND k.CODSUPERVISOR NOT IN (48, 56, 64, 81, 101, 132)
    AND k.CONDVENDA IN (1, 2, 3, 7, 9, 14, 15, 17, 18, 19, 98)
    AND k.NUMPED > 0"""

# ─── EQUIPE CASE ──────────────────────────────────────────────────────────────
def equipe_case(g_alias='g'):
    return f"""CASE
  WHEN UPPER(NVL({g_alias}.NOMEGERENTE,'')) LIKE '%MDLZ%' THEN 'Mondelez'
  WHEN UPPER(NVL({g_alias}.NOMEGERENTE,'')) LIKE '%BAHIA FORTE%'
    OR UPPER(NVL({g_alias}.NOMEGERENTE,'')) LIKE '% BF%'
    OR UPPER(NVL({g_alias}.NOMEGERENTE,'')) LIKE 'BF %' THEN 'Bahia Forte'
  WHEN UPPER(NVL({g_alias}.NOMEGERENTE,'')) LIKE '%CMA%' THEN 'CMA'
  WHEN UPPER(NVL({g_alias}.NOMEGERENTE,'')) LIKE '%ATACADO%'
    OR UPPER(NVL({g_alias}.NOMEGERENTE,'')) LIKE '%CABRAL%' THEN 'Atacado'
  ELSE 'Distribuição'
END"""

# ─── CTEs compartilhadas ──────────────────────────────────────────────────────
def base_ctes(kvf, mi, mf):
    """
    Retorna CTEs: dim_rca + meta_rca + meta_total
    K_VENDA escaneado UMA VEZ em dim_rca_vendas, reutilizado para vendas e custo.
    """
    mp = meta_periodo(mi, mf)
    return f"""
dim_rca_vendas AS (
    SELECT k.CODUSUR,
           MAX(k.CODGERENTE)    KEEP (DENSE_RANK LAST ORDER BY k.DTSAIDA) as CODGERENTE,
           MAX(k.CODSUPERVISOR) KEEP (DENSE_RANK LAST ORDER BY k.DTSAIDA) as CODSUPERVISOR,
           SUM(k.QTVENDIDA * k.PRECOUNITCONT) as VLTOTAL,
           SUM(k.QTVENDIDA * k.CUSTOREAL)     as VLCUSTO
    FROM CSOUSA.K_VENDA k
    WHERE 1=1 {KV_BASE} {kvf}
    GROUP BY k.CODUSUR
),
meta_rca AS (
    SELECT CODUSUR, SUM(VLVENDAPREV) as META_VALOR
    FROM CSOUSA.PCMETA
    WHERE TIPOMETA IN ('F','D')
    AND {mp}
    GROUP BY CODUSUR
),
meta_total AS (SELECT NVL(SUM(META_VALOR),0) as META FROM meta_rca)"""

# ─── MESES ────────────────────────────────────────────────────────────────────
MESES_2026 = [{"MES": f"2026-{str(m).zfill(2)}"} for m in range(1, 13)]

# ─── QUERIES ──────────────────────────────────────────────────────────────────

def SQL_RESUMO(kvf, mi, mf):
    return f"""
WITH {base_ctes(kvf, mi, mf)},
totais AS (
    SELECT
        ROUND(SUM(dv.VLTOTAL), 2)                                             as VLTOTAL,
        ROUND(SUM(dv.VLCUSTO), 2)                                             as VLCUSTO,
        ROUND(SUM(dv.VLTOTAL) - SUM(dv.VLCUSTO), 2)                          as MARGEM,
        ROUND((SUM(dv.VLTOTAL)-SUM(dv.VLCUSTO))/NULLIF(SUM(dv.VLTOTAL),0)*100,2) as PERC_MARGEM,
        COUNT(dv.CODUSUR)                                                     as RCAS_ATIVOS
    FROM dim_rca_vendas dv
)
SELECT
    t.VLTOTAL,
    t.VLCUSTO,
    t.MARGEM,
    t.PERC_MARGEM,
    t.RCAS_ATIVOS,
    mt.META,
    ROUND(t.VLTOTAL/NULLIF(mt.META,0)*100,1) as PERC_META
FROM totais t, meta_total mt
"""


def SQL_EVOLUCAO():
    return f"""
SELECT
    TO_CHAR(k.DTSAIDA,'YYYY-MM')                as MES,
    ROUND(SUM(k.QTVENDIDA * k.PRECOUNITCONT),2) as VLTOTAL,
    ROUND(SUM(k.QTVENDIDA * k.CUSTOREAL),2)     as VLCUSTO,
    ROUND(SUM(k.QTVENDIDA * k.PRECOUNITCONT)
         -SUM(k.QTVENDIDA * k.CUSTOREAL),2)     as MARGEM
FROM CSOUSA.K_VENDA k
WHERE k.DTSAIDA >= DATE '2026-01-01'
  AND k.DTSAIDA <  DATE '2027-01-01'
  {KV_BASE}
GROUP BY TO_CHAR(k.DTSAIDA,'YYYY-MM')
ORDER BY 1
"""


def SQL_EVOLUCAO_META():
    return """
SELECT TO_CHAR(DATA,'YYYY-MM') as MES, ROUND(SUM(VLVENDAPREV),2) as META
FROM CSOUSA.PCMETA
WHERE TIPOMETA IN ('F','D')
AND DATA >= DATE '2026-01-01' AND DATA < DATE '2027-01-01'
GROUP BY TO_CHAR(DATA,'YYYY-MM')
ORDER BY 1
"""


def SQL_POR_EQUIPE(kvf, mi, mf):
    ec = equipe_case('g')
    return f"""
WITH {base_ctes(kvf, mi, mf)}
SELECT
    {ec}                                                                  as EQUIPE,
    ROUND(SUM(dv.VLTOTAL),2)                                              as VLTOTAL,
    ROUND(SUM(dv.VLCUSTO),2)                                              as VLCUSTO,
    ROUND(SUM(dv.VLTOTAL)-SUM(dv.VLCUSTO),2)                             as MARGEM,
    ROUND((SUM(dv.VLTOTAL)-SUM(dv.VLCUSTO))/NULLIF(SUM(dv.VLTOTAL),0)*100,2) as PERC_MARGEM,
    COUNT(dv.CODUSUR)                                                     as RCAS,
    ROUND(NVL(SUM(mr.META_VALOR),0),2)                                    as META,
    ROUND(SUM(dv.VLTOTAL)/NULLIF(NVL(SUM(mr.META_VALOR),0),0)*100,1)     as PERC_META
FROM dim_rca_vendas dv
LEFT JOIN CSOUSA.PCGERENTE g ON g.CODGERENTE = dv.CODGERENTE
LEFT JOIN meta_rca mr        ON mr.CODUSUR   = dv.CODUSUR
GROUP BY {ec}
ORDER BY VLTOTAL DESC
"""


def SQL_POR_GERENTE(kvf, mi, mf, equipe=None):
    ec       = equipe_case('g')
    eq_filter= f"HAVING {ec} = '{equipe}'" if equipe else ""
    return f"""
WITH {base_ctes(kvf, mi, mf)}
SELECT
    {ec}                                                                  as EQUIPE,
    NVL(g.NOMEGERENTE,'Sem Gerente')                                      as GERENTE,
    dv.CODGERENTE,
    ROUND(SUM(dv.VLTOTAL),2)                                              as VLTOTAL,
    ROUND(SUM(dv.VLCUSTO),2)                                              as VLCUSTO,
    ROUND(SUM(dv.VLTOTAL)-SUM(dv.VLCUSTO),2)                             as MARGEM,
    ROUND((SUM(dv.VLTOTAL)-SUM(dv.VLCUSTO))/NULLIF(SUM(dv.VLTOTAL),0)*100,2) as PERC_MARGEM,
    COUNT(dv.CODUSUR)                                                     as RCAS,
    ROUND(NVL(SUM(mr.META_VALOR),0),2)                                    as META,
    ROUND(SUM(dv.VLTOTAL)/NULLIF(NVL(SUM(mr.META_VALOR),0),0)*100,1)     as PERC_META
FROM dim_rca_vendas dv
LEFT JOIN CSOUSA.PCGERENTE g ON g.CODGERENTE = dv.CODGERENTE
LEFT JOIN meta_rca mr        ON mr.CODUSUR   = dv.CODUSUR
WHERE dv.CODGERENTE IS NOT NULL
  AND NVL(g.NOMEGERENTE,'Sem Gerente') != 'Sem Gerente'
GROUP BY {ec}, g.NOMEGERENTE, dv.CODGERENTE
{eq_filter}
ORDER BY VLTOTAL DESC
"""


def SQL_POR_SUPERVISOR(kvf, mi, mf, codgerente=None, equipe=None):
    ec        = equipe_case('g')
    drill     = f"AND dv.CODGERENTE = {codgerente}" if codgerente else ""
    eq_filter = f"HAVING {ec} = '{equipe}'" if equipe else ""
    return f"""
WITH {base_ctes(kvf, mi, mf)}
SELECT
    {ec}                                                                  as EQUIPE,
    NVL(s.NOME,'Sem Supervisor')                                          as SUPERVISOR,
    dv.CODSUPERVISOR,
    NVL(g.NOMEGERENTE,'Sem Gerente')                                      as GERENTE,
    ROUND(SUM(dv.VLTOTAL),2)                                              as VLTOTAL,
    ROUND(SUM(dv.VLCUSTO),2)                                              as VLCUSTO,
    ROUND(SUM(dv.VLTOTAL)-SUM(dv.VLCUSTO),2)                             as MARGEM,
    ROUND((SUM(dv.VLTOTAL)-SUM(dv.VLCUSTO))/NULLIF(SUM(dv.VLTOTAL),0)*100,2) as PERC_MARGEM,
    COUNT(dv.CODUSUR)                                                     as RCAS,
    ROUND(NVL(SUM(mr.META_VALOR),0),2)                                    as META,
    ROUND(SUM(dv.VLTOTAL)/NULLIF(NVL(SUM(mr.META_VALOR),0),0)*100,1)     as PERC_META
FROM dim_rca_vendas dv
LEFT JOIN CSOUSA.PCUSUARI s  ON s.CODUSUR    = dv.CODSUPERVISOR
LEFT JOIN CSOUSA.PCGERENTE g ON g.CODGERENTE = dv.CODGERENTE
LEFT JOIN meta_rca mr        ON mr.CODUSUR   = dv.CODUSUR
WHERE dv.CODSUPERVISOR IS NOT NULL {drill}
GROUP BY {ec}, s.NOME, dv.CODSUPERVISOR, g.NOMEGERENTE
{eq_filter}
ORDER BY VLTOTAL DESC
"""


def SQL_POR_RCA(kvf, mi, mf, codsupervisor=None, codgerente=None, equipe=None):
    ec        = equipe_case('g')
    dsup      = f"AND dv.CODSUPERVISOR = {codsupervisor}" if codsupervisor else ""
    dger      = f"AND dv.CODGERENTE    = {codgerente}"    if codgerente    else ""
    eq_filter = f"AND {ec} = '{equipe}'"                  if equipe        else ""
    # RCA query: needs pedidos count — small extra scan but only per RCA
    return f"""
WITH {base_ctes(kvf, mi, mf)},
pedidos_rca AS (
    SELECT k.CODUSUR, COUNT(DISTINCT k.NUMPED) as PEDIDOS
    FROM CSOUSA.K_VENDA k
    WHERE 1=1 {KV_BASE} {kvf}
    GROUP BY k.CODUSUR
)
SELECT
    u.CODUSUR,
    u.NOME                                                                as RCA,
    {ec}                                                                  as EQUIPE,
    NVL(g.NOMEGERENTE,'Sem Gerente')                                      as GERENTE,
    NVL(s.NOME,'Sem Supervisor')                                          as SUPERVISOR,
    ROUND(dv.VLTOTAL,2)                                                   as VLTOTAL,
    ROUND(dv.VLCUSTO,2)                                                   as VLCUSTO,
    ROUND(dv.VLTOTAL - dv.VLCUSTO,2)                                      as MARGEM,
    ROUND((dv.VLTOTAL-dv.VLCUSTO)/NULLIF(dv.VLTOTAL,0)*100,2)            as PERC_MARGEM,
    NVL(pr.PEDIDOS,0)                                                     as PEDIDOS,
    NVL(mr.META_VALOR,0)                                                  as META,
    ROUND(dv.VLTOTAL/NULLIF(mr.META_VALOR,0)*100,1)                       as PERC_META
FROM dim_rca_vendas dv
JOIN  CSOUSA.PCUSUARI u      ON u.CODUSUR      = dv.CODUSUR
LEFT JOIN CSOUSA.PCGERENTE g ON g.CODGERENTE   = dv.CODGERENTE
LEFT JOIN CSOUSA.PCUSUARI s  ON s.CODUSUR      = dv.CODSUPERVISOR
LEFT JOIN pedidos_rca pr     ON pr.CODUSUR     = dv.CODUSUR
LEFT JOIN meta_rca mr        ON mr.CODUSUR     = dv.CODUSUR
WHERE 1=1 {dsup} {dger} {eq_filter}
ORDER BY dv.VLTOTAL DESC
FETCH FIRST 200 ROWS ONLY
"""


def SQL_FORNEC_POR_EQUIPE(kvf, equipe=None, codfornec=None):
    ec         = equipe_case('g')
    eq_filter  = f"AND {ec} = '{equipe}'"         if equipe    else ""
    fnc_filter = f"AND k.CODFORNEC = {codfornec}" if codfornec else ""
    limit      = "FETCH FIRST 60 ROWS ONLY"        if not codfornec else ""
    return f"""
WITH dim_rca AS (
    SELECT k.CODUSUR,
           MAX(k.CODGERENTE) KEEP (DENSE_RANK LAST ORDER BY k.DTSAIDA) as CODGERENTE
    FROM CSOUSA.K_VENDA k
    WHERE 1=1 {KV_BASE} {kvf}
    GROUP BY k.CODUSUR
)
SELECT
    f.CODFORNEC,
    f.FORNECEDOR,
    {ec}                                        as EQUIPE,
    ROUND(SUM(k.QTVENDIDA * k.PRECOUNITCONT),2) as VLTOTAL,
    COUNT(DISTINCT k.CODUSUR)                   as RCAS
FROM CSOUSA.K_VENDA k
LEFT JOIN dim_rca dr         ON dr.CODUSUR    = k.CODUSUR
LEFT JOIN CSOUSA.PCGERENTE g ON g.CODGERENTE  = dr.CODGERENTE
LEFT JOIN CSOUSA.PCFORNEC f  ON f.CODFORNEC   = k.CODFORNEC
WHERE 1=1 {KV_BASE} {kvf} {eq_filter} {fnc_filter}
GROUP BY f.CODFORNEC, f.FORNECEDOR, {ec}
ORDER BY VLTOTAL DESC
{limit}
"""


def SQL_FORNEC_POR_RCA(kvf, codusur):
    return f"""
SELECT
    f.CODFORNEC,
    f.FORNECEDOR,
    ROUND(SUM(k.QTVENDIDA * k.PRECOUNITCONT),2) as VLTOTAL
FROM CSOUSA.K_VENDA k
LEFT JOIN CSOUSA.PCFORNEC f ON f.CODFORNEC = k.CODFORNEC
WHERE k.CODUSUR = {codusur}
{KV_BASE} {kvf}
GROUP BY f.CODFORNEC, f.FORNECEDOR
ORDER BY VLTOTAL DESC
FETCH FIRST 30 ROWS ONLY
"""


def SQL_RCA_POR_FORNEC(kvf, codfornec):
    ec = equipe_case('g')
    return f"""
WITH dim_rca AS (
    SELECT k.CODUSUR,
           MAX(k.CODGERENTE)    KEEP (DENSE_RANK LAST ORDER BY k.DTSAIDA) as CODGERENTE,
           MAX(k.CODSUPERVISOR) KEEP (DENSE_RANK LAST ORDER BY k.DTSAIDA) as CODSUPERVISOR
    FROM CSOUSA.K_VENDA k
    WHERE 1=1 {KV_BASE} {kvf}
    GROUP BY k.CODUSUR
)
SELECT
    u.CODUSUR,
    u.NOME                                      as RCA,
    {ec}                                        as EQUIPE,
    NVL(g.NOMEGERENTE,'Sem Gerente')            as GERENTE,
    NVL(s.NOME,'Sem Supervisor')                as SUPERVISOR,
    ROUND(SUM(k.QTVENDIDA * k.PRECOUNITCONT),2) as VLTOTAL
FROM CSOUSA.K_VENDA k
JOIN  CSOUSA.PCUSUARI u    ON u.CODUSUR     = k.CODUSUR
LEFT JOIN dim_rca dr       ON dr.CODUSUR    = k.CODUSUR
LEFT JOIN CSOUSA.PCGERENTE g ON g.CODGERENTE = dr.CODGERENTE
LEFT JOIN CSOUSA.PCUSUARI s  ON s.CODUSUR    = dr.CODSUPERVISOR
WHERE k.CODFORNEC = {codfornec}
{KV_BASE} {kvf}
GROUP BY u.CODUSUR, u.NOME, {ec}, g.NOMEGERENTE, s.NOME
ORDER BY VLTOTAL DESC
FETCH FIRST 50 ROWS ONLY
"""


# ─── HTTP HANDLER ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        def p(k, default=None): return qs.get(k, [default])[0]

        mi  = p('mes_inicio')
        mf  = p('mes_fim')
        kvf = kv_periodo(mi, mf)

        # Cache key inclui todos os parâmetros relevantes
        ck = f"{path}|{mi}|{mf}|{p('equipe')}|{p('codgerente')}|{p('codsupervisor')}|{p('codfornec')}|{p('codusur')}"

        try:
            if path == '/api/meses':
                data = MESES_2026

            elif path == '/api/cache-clear':
                with _cache_lock:
                    _cache.clear()
                data = [{"status": "cleared"}]

            elif path == '/api/resumo':
                data = cached_query(ck, SQL_RESUMO(kvf, mi, mf))

            elif path == '/api/evolucao':
                cached = cache_get(ck)
                if cached is not None:
                    data = cached
                else:
                    vendas   = query(SQL_EVOLUCAO())
                    metas    = query(SQL_EVOLUCAO_META())
                    meta_map = {m['MES']: m['META'] for m in metas}
                    for row in vendas:
                        row['META'] = meta_map.get(row['MES'], 0)
                    data = vendas
                    cache_set(ck, data)

            elif path == '/api/por-equipe':
                data = cached_query(ck, SQL_POR_EQUIPE(kvf, mi, mf))

            elif path == '/api/por-gerente':
                data = cached_query(ck, SQL_POR_GERENTE(kvf, mi, mf, p('equipe')))

            elif path == '/api/por-supervisor':
                data = cached_query(ck, SQL_POR_SUPERVISOR(kvf, mi, mf, p('codgerente'), p('equipe')))

            elif path == '/api/por-rca':
                data = cached_query(ck, SQL_POR_RCA(kvf, mi, mf, p('codsupervisor'), p('codgerente'), p('equipe')))

            elif path == '/api/fornec-por-equipe':
                codfornec_v = p('codfornec')
                data = cached_query(ck, SQL_FORNEC_POR_EQUIPE(kvf, p('equipe'), int(codfornec_v) if codfornec_v else None))

            elif path == '/api/fornec-por-rca':
                codusur = p('codusur')
                if not codusur: raise ValueError("codusur obrigatório")
                data = cached_query(ck, SQL_FORNEC_POR_RCA(kvf, int(codusur)))

            elif path == '/api/rca-por-fornec':
                codfornec_v = p('codfornec')
                if not codfornec_v: raise ValueError("codfornec obrigatório")
                data = cached_query(ck, SQL_RCA_POR_FORNEC(kvf, int(codfornec_v)))

            else:
                self.send_error(404); return

            body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type',  'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        except Exception as e:
            err = json.dumps({'error': str(e), 'trace': traceback.format_exc()}).encode()
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(err)


class ThreadedHTTPServer(HTTPServer):
    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address), daemon=True)
        t.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


if __name__ == '__main__':
    PORT = 8742
    print(f'[API Vendas C&S] http://localhost:{PORT}')
    ThreadedHTTPServer(('localhost', PORT), Handler).serve_forever()
