import csv
import io
import os
import sqlite3
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

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
                aluno_nome TEXT NOT NULL,
                turma TEXT NOT NULL,
                intervalo TEXT NOT NULL,
                metodo_pagamento TEXT NOT NULL,
                troco_para REAL,
                total REAL NOT NULL,
                status TEXT NOT NULL,
                data_hora TEXT NOT NULL
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

        # Migrações automáticas de schema
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
        if "intervalo" not in cols_ped:
            cursor.execute("ALTER TABLE pedidos ADD COLUMN intervalo TEXT DEFAULT 'Manhã (09:45)'")
        if "turma" not in cols_ped:
            cursor.execute("ALTER TABLE pedidos ADD COLUMN turma TEXT DEFAULT ''")

        # Configurações padrão da escola e Pix
        cursor.execute("INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES ('escola_nome', 'Escola Estadual Prof. Aggeo Pereira do Amaral')")
        cursor.execute("INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES ('pix_chave', 'cantina.aggeo@escola.sp.gov.br')")
        cursor.execute("INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES ('pix_qrcode', '')")

        # Cardápio inicial com imagens padrão
        cursor.execute("SELECT COUNT(*) FROM produtos")
        if cursor.fetchone()[0] == 0:
            iniciais = [
                ("Pão de Queijo Tradicional", 4.50, "Quentinho, crocante e queijo da Canastra", "🥖", "https://images.unsplash.com/photo-1598182198871-d3f4ab4fd181?w=400&q=80", 35, 1),
                ("Coxinha de Frango com Catupiry", 7.00, "Massa dourada crocante com recheio cremoso", "🍗", "https://images.unsplash.com/photo-1541592106381-b31e9677c0e5?w=400&q=80", 25, 1),
                ("Enroladinho Misto Assado", 6.50, "Presunto especial, muçarela e orégano", "🥐", "https://images.unsplash.com/photo-1555507036-ab1f4038808a?w=400&q=80", 25, 1),
                ("Hambúrguer Artesanal Forno", 9.00, "Carne bovina, queijo cheddar e gergelim", "🍔", "https://images.unsplash.com/photo-1568901346375-23c9450c58cd?w=400&q=80", 20, 1),
                ("Suco Natural 300ml Laranja", 5.50, "100% fruta, bem gelado e sem adição de água", "🍊", "https://images.unsplash.com/photo-1613478223719-2ab802602423?w=400&q=80", 40, 1),
                ("Água Mineral 500ml", 3.50, "Geladíssima com ou sem gás", "💧", "https://images.unsplash.com/photo-1548839140-29a749e1bc4e?w=400&q=80", 50, 1)
            ]
            cursor.executemany(
                "INSERT INTO produtos (nome, preco, descricao, icone, imagem_url, estoque_dia, ativo) VALUES (?, ?, ?, ?, ?, ?, ?)",
                iniciais
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
    pix_qrcode: Optional[str] = ""

class ItemModel(BaseModel):
    produto_id: int
    nome: str
    qtd: int
    preco: float

class PedidoRequest(BaseModel):
    aluno_nome: str
    turma: str
    intervalo: str
    metodo_pagamento: str
    troco_para: Optional[float] = 0.0
    total: float
    itens: List[ItemModel]

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

# --- Configurações de Pix e QR Code ---
@app.get("/api/config/pix")
def obter_config_pix():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chave, valor FROM configuracoes WHERE chave IN ('pix_chave', 'pix_qrcode', 'escola_nome')")
        dados = dict(cursor.fetchall())
        return {
            "pix_chave": dados.get("pix_chave", ""),
            "pix_qrcode": dados.get("pix_qrcode", ""),
            "escola_nome": dados.get("escola_nome", "Escola Estadual Prof. Aggeo Pereira do Amaral")
        }

@app.post("/api/config/pix")
def salvar_config_pix(payload: PixConfigPayload):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        if payload.pix_chave is not None:
            cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('pix_chave', ?)", (payload.pix_chave.strip(),))
        if payload.pix_qrcode is not None:
            cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('pix_qrcode', ?)", (payload.pix_qrcode.strip(),))
        conn.commit()
    return {"status": "ok"}

# --- Gestão de Produtos ---
@app.get("/api/produtos")
def listar_produtos(apenas_ativos: bool = False):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        query = "SELECT * FROM produtos" + (" WHERE ativo = 1 ORDER BY nome ASC" if apenas_ativos else " ORDER BY ativo DESC, nome ASC")
        cursor.execute(query)
        return [dict(row) for row in cursor.fetchall()]

@app.post("/api/produtos")
def criar_produto(item: ProdutoPayload):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO produtos (nome, preco, descricao, icone, imagem_url, estoque_dia, ativo) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item.nome, item.preco, item.descricao, item.icone or "🥪", item.imagem_url or "", item.estoque_dia or 30, item.ativo if item.ativo is not None else 1)
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
            (item.nome, item.preco, item.descricao, item.icone or "🥪", item.imagem_url or "", item.estoque_dia or 0, item.ativo if item.ativo is not None else 1, prod_id)
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

# --- Pedidos ---
@app.post("/api/pedidos")
async def criar_pedido(pedido: PedidoRequest):
    order_id = f"P{uuid.uuid4().hex[:3].upper()}"
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status_inicial = "Pendente"

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        for item in pedido.itens:
            cursor.execute("SELECT nome, estoque_dia, ativo FROM produtos WHERE id = ?", (item.produto_id,))
            prod = cursor.fetchone()
            if not prod:
                raise HTTPException(status_code=400, detail=f"Item {item.nome} não encontrado.")
            if prod[2] == 0:
                raise HTTPException(status_code=400, detail=f"O item '{prod[0]}' foi pausado pela cantina.")
            if prod[1] < item.qtd:
                raise HTTPException(status_code=400, detail=f"Estoque insuficiente para '{prod[0]}'. Restam apenas {prod[1]} unidades.")

        cursor.execute(
            """INSERT INTO pedidos 
               (id, aluno_nome, turma, intervalo, metodo_pagamento, troco_para, total, status, data_hora) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, pedido.aluno_nome, pedido.turma, pedido.intervalo, pedido.metodo_pagamento, pedido.troco_para, pedido.total, status_inicial, agora)
        )

        for item in pedido.itens:
            cursor.execute(
                "INSERT INTO itens_pedido (pedido_id, produto_id, nome, qtd, preco) VALUES (?, ?, ?, ?, ?)",
                (order_id, item.produto_id, item.nome, item.qtd, item.preco)
            )
            cursor.execute(
                "UPDATE produtos SET estoque_dia = estoque_dia - ? WHERE id = ?",
                (item.qtd, item.produto_id)
            )
        conn.commit()

    itens_formatados = [
        item.model_dump() if hasattr(item, "model_dump") else item.dict()
        for item in pedido.itens
    ]

    dados_notificacao = {
        "id": order_id,
        "aluno_nome": pedido.aluno_nome,
        "turma": pedido.turma,
        "intervalo": pedido.intervalo,
        "metodo_pagamento": pedido.metodo_pagamento,
        "troco_para": pedido.troco_para,
        "total": pedido.total,
        "status": status_inicial,
        "horario": datetime.now().strftime("%H:%M"),
        "itens": itens_formatados
    }

    await notifier.notify_all(dados_notificacao)

    return {
        "codigo_retirada": order_id,
        "metodo_pagamento": pedido.metodo_pagamento,
        "total": pedido.total,
        "troco_para": pedido.troco_para
    }

@app.get("/api/pedidos/ativos")
def listar_pedidos_ativos():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pedidos WHERE status != 'Entregue' ORDER BY data_hora DESC")
        pedidos = cursor.fetchall()
        
        resultado = []
        for p in pedidos:
            cursor.execute("SELECT produto_id, nome, qtd, preco FROM itens_pedido WHERE pedido_id = ?", (p["id"],))
            itens = [dict(row) for row in cursor.fetchall()]
            resultado.append({
                "id": p["id"],
                "aluno_nome": p["aluno_nome"],
                "turma": p["turma"],
                "intervalo": p["intervalo"],
                "metodo_pagamento": p["metodo_pagamento"],
                "troco_para": p["troco_para"],
                "total": p["total"],
                "status": p["status"],
                "horario": p["data_hora"].split(" ")[1][:5],
                "itens": itens
            })
        return resultado

@app.patch("/api/pedidos/{pedido_id}/status")
def atualizar_status(pedido_id: str, status: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE pedidos SET status = ? WHERE id = ?", (status, pedido_id))
        conn.commit()
    return {"status": "ok"}

# --- Relatórios & Demandas ---
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
            WHERE p.status != 'Cancelado'
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
            WHERE p.status != 'Cancelado'
            GROUP BY p.intervalo
        """)
        turnos = [dict(row) for row in cursor.fetchall()]

        return {
            "resumo": {
                "total_itens_vendidos": total_geral_unidades if itens else 0,
                "faturamento_total": total_geral_receita
            },
            "itens": itens,
            "turnos": turnos
        }

@app.get("/api/relatorios/demanda/csv")
def baixar_relatorio_csv():
    dados = obter_dados_demanda()
    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';')
    
    writer.writerow(["Ranking", "Produto", "Qtd Vendida", "% Demanda", "Faturamento Total (R$)", "Nº de Pedidos"])
    for idx, item in enumerate(dados["itens"], 1):
        writer.writerow([
            idx,
            item["nome"],
            item["total_unidades"],
            f"{item['participacao_pct']}%",
            f"{item['receita_total']:.2f}".replace('.', ','),
            item["pedidos_presentes"]
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=demanda_aggeo_{datetime.now().strftime('%Y%m%d')}.csv"}
    )