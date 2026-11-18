import csv
import io
import json
import os
import re
import sqlite3
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

app = FastAPI(title="Cantina Express Cyberpunk - E.E. Prof. Aggeo Pereira do Amaral")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "cantina.db")


# --- Gerador Oficial de BR Code EMV Pix (Padrão Banco Central) ---
def limpar_texto_emv(texto: str, max_len: int) -> str:
    nfkd = unicodedata.normalize("NFKD", texto or "")
    ascii_str = "".join([c for c in nfkd if not unicodedata.combining(c)])
    ascii_str = re.sub(r"[^A-Za-z0-9 ]", "", ascii_str).upper().strip()
    return ascii_str[:max_len] or "CANTINA"


def crc16_ccitt(payload: str) -> str:
    crc = 0xFFFF
    for byte in payload.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def gerar_payload_pix(
    chave: str,
    valor: float,
    txid: str,
    recebedor: str = "CANTINA AGGEO",
    cidade: str = "SOROCABA",
) -> str:
    chave_limpa = (chave or "cantina.aggeo@escola.sp.gov.br").strip()
    gui = "0014br.gov.bcb.pix"
    campo_chave = f"01{len(chave_limpa):02d}{chave_limpa}"
    mai = f"{gui}{campo_chave}"
    campo_26 = f"26{len(mai):02d}{mai}"

    valor_str = f"{valor:.2f}"
    campo_54 = f"54{len(valor_str):02d}{valor_str}"

    rec_limpo = limpar_texto_emv(recebedor, 25)
    cid_limpo = limpar_texto_emv(cidade, 15)
    txid_limpo = re.sub(r"[^A-Za-z0-9]", "", txid).upper()[:25] or "CANTINA01"
    campo_62_sub = f"05{len(txid_limpo):02d}{txid_limpo}"
    campo_62 = f"62{len(campo_62_sub):02d}{campo_62_sub}"

    sem_crc = (
        f"000201{campo_26}520400005303986{campo_54}5802BR"
        f"59{len(rec_limpo):02d}{rec_limpo}60{len(cid_limpo):02d}{cid_limpo}"
        f"{campo_62}6304"
    )
    return f"{sem_crc}{crc16_ccitt(sem_crc)}"


# --- Banco de Dados SQLite ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                preco REAL NOT NULL,
                descricao TEXT,
                icone TEXT DEFAULT '🥪',
                imagem_url TEXT DEFAULT '',
                estoque_dia INTEGER DEFAULT 30,
                ativo INTEGER DEFAULT 1
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pedidos (
                id TEXT PRIMARY KEY,
                tipo_usuario TEXT DEFAULT 'Aluno',
                aluno_nome TEXT NOT NULL,
                turma TEXT NOT NULL,
                intervalo TEXT NOT NULL,
                metodo_pagamento TEXT NOT NULL,
                pagamento_status TEXT DEFAULT 'pending',
                mp_payment_id TEXT DEFAULT '',
                pix_payload TEXT DEFAULT '',
                pix_qrcode_base64 TEXT DEFAULT '',
                total REAL NOT NULL,
                status TEXT NOT NULL,
                data_hora TEXT NOT NULL,
                entregue_em TEXT DEFAULT ''
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS itens_pedido (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id TEXT NOT NULL,
                produto_id INTEGER NOT NULL,
                nome TEXT NOT NULL,
                qtd INTEGER NOT NULL,
                preco REAL NOT NULL,
                FOREIGN KEY (pedido_id) REFERENCES pedidos(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS configuracoes (
                chave TEXT PRIMARY KEY,
                valor TEXT
            )
        """)

        # Migrações defensivas
        cursor.execute("PRAGMA table_info(produtos)")
        cols_prod = [c[1] for c in cursor.fetchall()]
        if "imagem_url" not in cols_prod:
            cursor.execute("ALTER TABLE produtos ADD COLUMN imagem_url TEXT DEFAULT ''")
        if "estoque_dia" not in cols_prod:
            cursor.execute("ALTER TABLE produtos ADD COLUMN estoque_dia INTEGER DEFAULT 30")
        if "ativo" not in cols_prod:
            cursor.execute("ALTER TABLE produtos ADD COLUMN ativo INTEGER DEFAULT 1")

        cursor.execute("PRAGMA table_info(pedidos)")
        cols_ped = [c[1] for c in cursor.fetchall()]
        if "tipo_usuario" not in cols_ped:
            cursor.execute("ALTER TABLE pedidos ADD COLUMN tipo_usuario TEXT DEFAULT 'Aluno'")
        if "pagamento_status" not in cols_ped:
            cursor.execute("ALTER TABLE pedidos ADD COLUMN pagamento_status TEXT DEFAULT 'pending'")
        if "mp_payment_id" not in cols_ped:
            cursor.execute("ALTER TABLE pedidos ADD COLUMN mp_payment_id TEXT DEFAULT ''")
        if "pix_payload" not in cols_ped:
            cursor.execute("ALTER TABLE pedidos ADD COLUMN pix_payload TEXT DEFAULT ''")
        if "pix_qrcode_base64" not in cols_ped:
            cursor.execute("ALTER TABLE pedidos ADD COLUMN pix_qrcode_base64 TEXT DEFAULT ''")
        if "entregue_em" not in cols_ped:
            cursor.execute("ALTER TABLE pedidos ADD COLUMN entregue_em TEXT DEFAULT ''")

        # Configurações iniciais padrão
        defaults = [
            ("escola_nome", "Escola Estadual Prof. Aggeo Pereira do Amaral"),
            ("escola_logo", ""),
            ("pix_chave", "cantina.aggeo@escola.sp.gov.br"),
            ("pix_recebedor", "CANTINA AGGEO"),
            ("pix_cidade", "SOROCABA"),
            ("mp_access_token", ""),
        ]
        for k, v in defaults:
            cursor.execute("INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES (?, ?)", (k, v))

        # Carga padrão de produtos
        cursor.execute("SELECT COUNT(*) FROM produtos")
        if cursor.fetchone()[0] == 0:
            iniciais = [
                ("Pão de Queijo Tradicional", 4.50, "Quentinho, crocante e queijo da Canastra", "🥖", "https://images.unsplash.com/photo-1598182198871-d3f4ab4fd181?w=400&q=80", 35, 1),
                ("Coxinha de Frango com Catupiry", 7.00, "Massa dourada crocante com recheio cremoso", "🍗", "https://images.unsplash.com/photo-1541592106381-b31e9677c0e5?w=400&q=80", 25, 1),
                ("Enroladinho Misto Assado", 6.50, "Presunto especial, muçarela e orégano", "🥐", "https://images.unsplash.com/photo-1555507036-ab1f4038808a?w=400&q=80", 25, 1),
                ("Hambúrguer Artesanal Forno", 9.00, "Carne bovina, queijo cheddar e gergelim", "🍔", "https://images.unsplash.com/photo-1568901346375-23c9450c58cd?w=400&q=80", 20, 1),
                ("Suco Natural 300ml Laranja", 5.50, "100% fruta, bem gelado e sem adição de água", "🍊", "https://images.unsplash.com/photo-1613478223719-2ab802602423?w=400&q=80", 40, 1),
                ("Água Mineral 500ml", 3.50, "Geladíssima com ou sem gás", "💧", "https://images.unsplash.com/photo-1548839140-29a749e1bc4e?w=400&q=80", 50, 1),
            ]
            cursor.executemany(
                "INSERT INTO produtos (nome, preco, descricao, icone, imagem_url, estoque_dia, ativo) VALUES (?, ?, ?, ?, ?, ?, ?)",
                iniciais,
            )
        conn.commit()


init_db()


class KitchenNotifier:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)

    async def notify_all(self, payload: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(payload)
            except Exception:
                pass


notifier = KitchenNotifier()


# --- Modelos Pydantic ---
class ProdutoPayload(BaseModel):
    nome: str
    preco: float
    descricao: Optional[str] = ""
    icone: Optional[str] = "🥪"
    imagem_url: Optional[str] = ""
    estoque_dia: Optional[int] = 30
    ativo: Optional[int] = 1


class AtualizaEstoquePayload(BaseModel):
    estoque_dia: int


class PixConfigPayload(BaseModel):
    pix_chave: Optional[str] = ""
    pix_recebedor: Optional[str] = "CANTINA AGGEO"
    pix_cidade: Optional[str] = "SOROCABA"
    mp_access_token: Optional[str] = ""


class LogoConfigPayload(BaseModel):
    escola_logo: str


class ItemModel(BaseModel):
    produto_id: int
    nome: str
    qtd: int
    preco: float


class PedidoRequest(BaseModel):
    tipo_usuario: Optional[str] = "Aluno"
    aluno_nome: str
    turma: str
    intervalo: str
    itens: List[ItemModel]

    @field_validator("aluno_nome")
    @classmethod
    def validar_nome(cls, v: str):
        if not v or not v.strip():
            raise ValueError("O nome completo é obrigatório.")
        return v.strip()

    @field_validator("turma")
    @classmethod
    def validar_turma(cls, v: str):
        if not v or not v.strip():
            raise ValueError("O campo de Série/Turma ou Disciplina/Sala é obrigatório.")
        return v.strip()


# --- Integração Mercado Pago Pix ---
def criar_cobranca_mercadopago(access_token: str, order_id: str, total: float, cliente_nome: str):
    url = "https://api.mercadopago.com/v1/payments"
    body = {
        "transaction_amount": round(float(total), 2),
        "description": f"Cantina E.E. Prof. Aggeo - Pedido {order_id}",
        "payment_method_id": "pix",
        "external_reference": order_id,
        "payer": {
            "email": f"pedido.{order_id.lower()}@cantinaaggeo.com.br",
            "first_name": (cliente_nome or "Cliente")[:30],
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "CantinaExpress/1.0",
            "X-Idempotency-Key": f"cantina-{order_id}-{uuid.uuid4().hex[:8]}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            poi = data.get("point_of_interaction", {}).get("transaction_data", {})
            return {
                "mp_payment_id": str(data.get("id", "")),
                "pix_payload": poi.get("qr_code", ""),
                "pix_qrcode_base64": poi.get("qr_code_base64", ""),
            }
    except urllib.error.HTTPError as e:
        detalhe = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Erro Mercado Pago ({e.code}): {detalhe}")


def consultar_status_mercadopago(access_token: str, mp_payment_id: str) -> str:
    url = f"https://api.mercadopago.com/v1/payments/{mp_payment_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "CantinaExpress/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("status", "pending")


async def confirmar_pagamento_pedido(pedido_id: str) -> bool:
    """Confirma o pagamento, baixa estoque e envia pedido para a fila de preparo da cantina."""
    notificar_payload = None

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
        ped = cursor.fetchone()
        if not ped:
            return False

        if ped["pagamento_status"] == "approved":
            return True

        cursor.execute("SELECT produto_id, nome, qtd, preco FROM itens_pedido WHERE pedido_id = ?", (pedido_id,))
        itens = [dict(row) for row in cursor.fetchall()]

        # Baixa no estoque somente após confirmação do Pix
        for it in itens:
            cursor.execute(
                "UPDATE produtos SET estoque_dia = MAX(0, estoque_dia - ?) WHERE id = ?",
                (it["qtd"], it["produto_id"]),
            )

        cursor.execute(
            "UPDATE pedidos SET pagamento_status = 'approved', status = 'Pendente' WHERE id = ?",
            (pedido_id,),
        )
        conn.commit()

        notificar_payload = {
            "evento": "pagamento_confirmado",
            "id": ped["id"],
            "tipo_usuario": ped["tipo_usuario"],
            "aluno_nome": ped["aluno_nome"],
            "turma": ped["turma"],
            "intervalo": ped["intervalo"],
            "metodo_pagamento": "Pix",
            "pagamento_status": "approved",
            "total": ped["total"],
            "status": "Pendente",
            "horario": ped["data_hora"].split(" ")[1][:5],
            "itens": itens,
        }

    if notificar_payload:
        await notifier.notify_all(notificar_payload)

    return True


# --- Rotas de Páginas ---
@app.get("/")
def pagina_aluno():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@app.get("/cantina")
def pagina_cantina():
    return FileResponse(os.path.join(BASE_DIR, "cantina.html"))


@app.websocket("/ws/pedidos")
async def ws_pedidos(ws: WebSocket):
    await notifier.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        notifier.disconnect(ws)


# --- Configurações Gerais ---
@app.get("/api/config/geral")
def obter_config_geral():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chave, valor FROM configuracoes")
        dados = dict(cursor.fetchall())
        token = dados.get("mp_access_token", "")
        return {
            "escola_nome": dados.get("escola_nome", "Escola Estadual Prof. Aggeo Pereira do Amaral"),
            "escola_logo": dados.get("escola_logo", ""),
            "pix_chave": dados.get("pix_chave", "cantina.aggeo@escola.sp.gov.br"),
            "pix_recebedor": dados.get("pix_recebedor", "CANTINA AGGEO"),
            "pix_cidade": dados.get("pix_cidade", "SOROCABA"),
            "mp_configurado": bool(token.strip()),
            "mp_access_token_mascarado": f"{token[:12]}..." if len(token) > 12 else "",
        }


@app.post("/api/config/logo")
def salvar_config_logo(payload: LogoConfigPayload):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('escola_logo', ?)",
            (payload.escola_logo.strip(),),
        )
        conn.commit()
    return {"status": "ok"}


@app.post("/api/config/pix")
def salvar_config_pix(payload: PixConfigPayload):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        if payload.pix_chave is not None:
            cursor.execute(
                "INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('pix_chave', ?)",
                (payload.pix_chave.strip(),),
            )
        if payload.pix_recebedor is not None:
            cursor.execute(
                "INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('pix_recebedor', ?)",
                (payload.pix_recebedor.strip(),),
            )
        if payload.pix_cidade is not None:
            cursor.execute(
                "INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('pix_cidade', ?)",
                (payload.pix_cidade.strip(),),
            )
        if payload.mp_access_token is not None and payload.mp_access_token.strip() != "":
            cursor.execute(
                "INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('mp_access_token', ?)",
                (payload.mp_access_token.strip(),),
            )
        conn.commit()
    return {"status": "ok"}


# --- Gestão de Produtos ---
@app.get("/api/produtos")
def listar_produtos(apenas_ativos: bool = False):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        query = "SELECT * FROM produtos" + (
            " WHERE ativo = 1 ORDER BY nome ASC" if apenas_ativos else " ORDER BY ativo DESC, nome ASC"
        )
        cursor.execute(query)
        return [dict(row) for row in cursor.fetchall()]


@app.post("/api/produtos")
def criar_produto(item: ProdutoPayload):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO produtos (nome, preco, descricao, icone, imagem_url, estoque_dia, ativo) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                item.nome,
                item.preco,
                item.descricao,
                item.icone or "🥪",
                item.imagem_url or "",
                item.estoque_dia or 30,
                item.ativo if item.ativo is not None else 1,
            ),
        )
        conn.commit()
        novo_id = cursor.lastrowid
    return {"status": "ok", "id": novo_id}


@app.put("/api/produtos/{prod_id}")
def editar_produto(prod_id: int, item: ProdutoPayload):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE produtos 
               SET nome = ?, preco = ?, descricao = ?, icone = ?, imagem_url = ?, estoque_dia = ?, ativo = ?
               WHERE id = ?""",
            (
                item.nome,
                item.preco,
                item.descricao,
                item.icone or "🥪",
                item.imagem_url or "",
                item.estoque_dia or 0,
                item.ativo if item.ativo is not None else 1,
                prod_id,
            ),
        )
        conn.commit()
    return {"status": "ok"}


@app.delete("/api/produtos/{prod_id}")
def deletar_produto(prod_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM produtos WHERE id = ?", (prod_id,))
        conn.commit()
    return {"status": "ok"}


@app.patch("/api/produtos/{prod_id}/toggle")
def alternar_status_produto(prod_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE produtos SET ativo = CASE WHEN ativo = 1 THEN 0 ELSE 1 END WHERE id = ?", (prod_id,))
        conn.commit()
    return {"status": "ok"}


@app.patch("/api/produtos/{prod_id}/estoque")
def atualizar_estoque_produto(prod_id: int, dados: AtualizaEstoquePayload):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE produtos SET estoque_dia = ? WHERE id = ?", (dados.estoque_dia, prod_id))
        conn.commit()
    return {"status": "ok"}


# --- Fluxo de Criação de Pedido + Geração de QR Code Pix Dinâmico ---
@app.post("/api/pedidos")
async def criar_pedido(pedido: PedidoRequest):
    if not pedido.itens:
        raise HTTPException(status_code=400, detail="Selecione ao menos um item no cardápio.")

    order_id = f"P{uuid.uuid4().hex[:3].upper()}"
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_calculado = 0.0

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        # Recalcula o total direto do banco para segurança e valida estoque
        itens_validados = []
        for item in pedido.itens:
            cursor.execute("SELECT nome, preco, estoque_dia, ativo FROM produtos WHERE id = ?", (item.produto_id,))
            prod = cursor.fetchone()
            if not prod:
                raise HTTPException(status_code=400, detail=f"Item {item.nome} não encontrado.")
            if prod[3] == 0:
                raise HTTPException(status_code=400, detail=f"O item '{prod[0]}' foi pausado pela cantina.")
            if prod[2] < item.qtd:
                raise HTTPException(
                    status_code=400,
                    detail=f"Estoque insuficiente para '{prod[0]}'. Restam apenas {prod[2]} unidades.",
                )
            preco_unitario = float(prod[1])
            total_calculado += preco_unitario * item.qtd
            itens_validados.append((item.produto_id, prod[0], item.qtd, preco_unitario))

        total_calculado = round(total_calculado, 2)

        cursor.execute("SELECT chave, valor FROM configuracoes")
        cfg = dict(cursor.fetchall())
        mp_token = cfg.get("mp_access_token", "").strip()
        pix_chave = cfg.get("pix_chave", "cantina.aggeo@escola.sp.gov.br").strip()
        pix_recebedor = cfg.get("pix_recebedor", "CANTINA AGGEO").strip()
        pix_cidade = cfg.get("pix_cidade", "SOROCABA").strip()

        mp_payment_id = ""
        pix_payload = ""
        pix_qrcode_base64 = ""
        modo_gateway = "emv_direto"

        if mp_token:
            try:
                mp_res = criar_cobranca_mercadopago(mp_token, order_id, total_calculado, pedido.aluno_nome)
                mp_payment_id = mp_res["mp_payment_id"]
                pix_payload = mp_res["pix_payload"]
                pix_qrcode_base64 = mp_res["pix_qrcode_base64"]
                modo_gateway = "mercadopago"
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Falha ao gerar cobrança Pix no Mercado Pago: {exc}",
                )
        else:
            pix_payload = gerar_payload_pix(pix_chave, total_calculado, order_id, pix_recebedor, pix_cidade)

        # Pedido nasce como Aguardando Pagamento (vai para aba Aguardando Conferência)
        cursor.execute(
            """INSERT INTO pedidos 
               (id, tipo_usuario, aluno_nome, turma, intervalo, metodo_pagamento, pagamento_status,
                mp_payment_id, pix_payload, pix_qrcode_base64, total, status, data_hora, entregue_em) 
               VALUES (?, ?, ?, ?, ?, 'Pix', 'pending', ?, ?, ?, ?, 'Aguardando Pagamento', ?, '')""",
            (
                order_id,
                pedido.tipo_usuario or "Aluno",
                pedido.aluno_nome,
                pedido.turma,
                pedido.intervalo,
                mp_payment_id,
                pix_payload,
                pix_qrcode_base64,
                total_calculado,
                agora,
            ),
        )

        itens_lista = []
        for prod_id, nome_prod, qtd_prod, preco_prod in itens_validados:
            cursor.execute(
                "INSERT INTO itens_pedido (pedido_id, produto_id, nome, qtd, preco) VALUES (?, ?, ?, ?, ?)",
                (order_id, prod_id, nome_prod, qtd_prod, preco_prod),
            )
            itens_lista.append({
                "produto_id": prod_id,
                "nome": nome_prod,
                "qtd": qtd_prod,
                "preco": preco_prod,
            })

        conn.commit()

    # Notifica o painel da cantina em tempo real sobre novo pedido aguardando conferência
    await notifier.notify_all({
        "evento": "novo_pendente",
        "id": order_id,
        "tipo_usuario": pedido.tipo_usuario or "Aluno",
        "aluno_nome": pedido.aluno_nome,
        "turma": pedido.turma,
        "intervalo": pedido.intervalo,
        "total": total_calculado,
        "horario": agora.split(" ")[1][:5],
        "itens": itens_lista,
    })

    return {
        "pedido_id": order_id,
        "total": total_calculado,
        "pix_payload": pix_payload,
        "pix_qrcode_base64": pix_qrcode_base64,
        "modo_gateway": modo_gateway,
    }


@app.get("/api/pedidos/{pedido_id}/verificar-pagamento")
async def verificar_pagamento_pedido(pedido_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
        ped = cursor.fetchone()
        if not ped:
            raise HTTPException(status_code=404, detail="Pedido não encontrado.")

        if ped["status"] == "Cancelado":
            return {"pago": False, "status": "Cancelado"}

        if ped["pagamento_status"] == "approved":
            return {
                "pago": True,
                "codigo_retirada": ped["id"],
                "total": ped["total"],
                "intervalo": ped["intervalo"],
            }

        cursor.execute("SELECT valor FROM configuracoes WHERE chave = 'mp_access_token'")
        row_tk = cursor.fetchone()
        mp_token = row_tk["valor"].strip() if row_tk and row_tk["valor"] else ""
        mp_payment_id = ped["mp_payment_id"]

    if not mp_token or not mp_payment_id:
        return {
            "pago": False,
            "status": "aguardando_conferencia",
        }

    try:
        status_mp = consultar_status_mercadopago(mp_token, mp_payment_id)
        if status_mp == "approved":
            await confirmar_pagamento_pedido(pedido_id)
            return {
                "pago": True,
                "codigo_retirada": pedido_id,
                "total": ped["total"],
                "intervalo": ped["intervalo"],
            }
        return {"pago": False, "status": status_mp}
    except Exception as exc:
        return {"pago": False, "status": "erro_api", "detalhe": str(exc)}


@app.post("/api/pagamentos/confirmar/{pedido_id}")
async def confirmar_pagamento_endpoint(pedido_id: str):
    """Confirma o Pix (via webhook ou conferência manual da cantina) e libera o código ao cliente."""
    ok = await confirmar_pagamento_pedido(pedido_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")
    return {"status": "approved", "pedido_id": pedido_id}


@app.patch("/api/pedidos/{pedido_id}/cancelar")
def cancelar_pedido_pendente(pedido_id: str):
    """Cancela/recusa um pedido pendente na aba Aguardando Conferência."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pedidos SET status = 'Cancelado' WHERE id = ? AND pagamento_status != 'approved'",
            (pedido_id,),
        )
        conn.commit()
    return {"status": "Cancelado", "pedido_id": pedido_id}


@app.post("/api/webhooks/mercadopago")
async def webhook_mercadopago(request: Request):
    """Recebe notificações instantâneas do Mercado Pago quando o Pix é liquidado."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    payment_id = (
        str(body.get("data", {}).get("id", ""))
        or request.query_params.get("data.id", "")
        or request.query_params.get("id", "")
    )
    if not payment_id:
        return {"status": "ignored"}

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT valor FROM configuracoes WHERE chave = 'mp_access_token'")
        row_tk = cursor.fetchone()
        mp_token = row_tk["valor"].strip() if row_tk and row_tk["valor"] else ""

        cursor.execute("SELECT id FROM pedidos WHERE mp_payment_id = ?", (payment_id,))
        row_ped = cursor.fetchone()

    if mp_token and row_ped:
        try:
            status_mp = consultar_status_mercadopago(mp_token, payment_id)
            if status_mp == "approved":
                await confirmar_pagamento_pedido(row_ped["id"])
        except Exception:
            pass

    return {"status": "ok"}


# --- Listagem de Pedidos: Aguardando Conferência, Pagos Ativos & Histórico ---
@app.get("/api/pedidos/pendentes")
def listar_pedidos_pendentes():
    """Retorna pedidos aguardando confirmação de Pix para a aba Aguardando Conferência."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM pedidos 
               WHERE pagamento_status = 'pending' AND status = 'Aguardando Pagamento' 
               ORDER BY data_hora DESC"""
        )
        pedidos = cursor.fetchall()

        resultado = []
        for p in pedidos:
            cursor.execute("SELECT produto_id, nome, qtd, preco FROM itens_pedido WHERE pedido_id = ?", (p["id"],))
            itens = [dict(row) for row in cursor.fetchall()]
            resultado.append({
                "id": p["id"],
                "tipo_usuario": p["tipo_usuario"],
                "aluno_nome": p["aluno_nome"],
                "turma": p["turma"],
                "intervalo": p["intervalo"],
                "metodo_pagamento": p["metodo_pagamento"],
                "pagamento_status": p["pagamento_status"],
                "total": p["total"],
                "status": p["status"],
                "horario": p["data_hora"].split(" ")[1][:5],
                "data_hora": p["data_hora"],
                "itens": itens,
            })
        return resultado


@app.get("/api/pedidos/ativos")
def listar_pedidos_ativos():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM pedidos 
               WHERE pagamento_status = 'approved' AND status IN ('Pendente', 'Pronto para Retirada') 
               ORDER BY data_hora DESC"""
        )
        pedidos = cursor.fetchall()

        resultado = []
        for p in pedidos:
            cursor.execute("SELECT produto_id, nome, qtd, preco FROM itens_pedido WHERE pedido_id = ?", (p["id"],))
            itens = [dict(row) for row in cursor.fetchall()]
            resultado.append({
                "id": p["id"],
                "tipo_usuario": p["tipo_usuario"],
                "aluno_nome": p["aluno_nome"],
                "turma": p["turma"],
                "intervalo": p["intervalo"],
                "metodo_pagamento": p["metodo_pagamento"],
                "pagamento_status": p["pagamento_status"],
                "total": p["total"],
                "status": p["status"],
                "horario": p["data_hora"].split(" ")[1][:5],
                "itens": itens,
            })
        return resultado


@app.get("/api/pedidos/historico")
def listar_pedidos_historico():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM pedidos 
               WHERE status = 'Entregue' 
               ORDER BY entregue_em DESC, data_hora DESC"""
        )
        pedidos = cursor.fetchall()

        resultado = []
        for p in pedidos:
            cursor.execute("SELECT produto_id, nome, qtd, preco FROM itens_pedido WHERE pedido_id = ?", (p["id"],))
            itens = [dict(row) for row in cursor.fetchall()]
            resultado.append({
                "id": p["id"],
                "tipo_usuario": p["tipo_usuario"],
                "aluno_nome": p["aluno_nome"],
                "turma": p["turma"],
                "intervalo": p["intervalo"],
                "total": p["total"],
                "data_hora": p["data_hora"],
                "entregue_em": p["entregue_em"] or p["data_hora"],
                "itens": itens,
            })
        return resultado


@app.patch("/api/pedidos/{pedido_id}/status")
def atualizar_status(pedido_id: str, status: str):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        if status == "Entregue":
            cursor.execute(
                "UPDATE pedidos SET status = 'Entregue', entregue_em = ? WHERE id = ?",
                (agora, pedido_id),
            )
        else:
            cursor.execute("UPDATE pedidos SET status = ? WHERE id = ?", (status, pedido_id))
        conn.commit()
    return {"status": "ok"}


# --- Relatórios, Gráficos e Reset de Estatísticas ---
@app.get("/api/relatorios/demanda")
def obter_dados_demanda():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT 
                ip.nome,
                SUM(ip.qtd) as total_unidades,
                SUM(ip.qtd * ip.preco) as receita_total,
                COUNT(DISTINCT ip.pedido_id) as pedidos_presentes
            FROM itens_pedido ip
            JOIN pedidos p ON ip.pedido_id = p.id
            WHERE p.pagamento_status = 'approved' AND p.status != 'Cancelado'
            GROUP BY ip.nome
            ORDER BY total_unidades DESC
        """)
        itens = [dict(row) for row in cursor.fetchall()]

        total_geral_unidades = sum(i["total_unidades"] for i in itens) or 1
        total_geral_receita = sum(i["receita_total"] for i in itens)

        for i in itens:
            i["participacao_pct"] = round((i["total_unidades"] / total_geral_unidades) * 100, 1)

        cursor.execute("""
            SELECT p.intervalo, COUNT(p.id) as total_pedidos, SUM(p.total) as total_faturado
            FROM pedidos p
            WHERE p.pagamento_status = 'approved' AND p.status != 'Cancelado'
            GROUP BY p.intervalo
        """)
        turnos = [dict(row) for row in cursor.fetchall()]

        return {
            "resumo": {
                "total_itens_vendidos": total_geral_unidades if itens else 0,
                "faturamento_total": total_geral_receita,
            },
            "itens": itens,
            "turnos": turnos,
        }


@app.post("/api/relatorios/zerar")
def zerar_dados_relatorios():
    """Apaga o histórico de vendas e pedidos concluídos para reiniciar os gráficos."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM itens_pedido")
        cursor.execute("DELETE FROM pedidos")
        conn.commit()
    return {"status": "ok", "mensagem": "Dados dos relatórios zerados com sucesso."}


@app.get("/api/relatorios/demanda/csv")
def baixar_relatorio_csv():
    dados = obter_dados_demanda()
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")

    writer.writerow(["Ranking", "Produto", "Qtd Vendida", "% Demanda", "Faturamento Total (R$)", "Nº de Pedidos"])
    for idx, item in enumerate(dados["itens"], 1):
        writer.writerow([
            idx,
            item["nome"],
            item["total_unidades"],
            f"{item['participacao_pct']}%",
            f"{item['receita_total']:.2f}".replace(".", ","),
            item["pedidos_presentes"],
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=demanda_aggeo_{datetime.now().strftime('%Y%m%d')}.csv"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
