import urllib.request
import urllib.error
import argparse
import binascii
import json
import os
import secrets
import sqlite3
import threading
import webbrowser
import zipfile
from contextlib import contextmanager
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from xml.etree.ElementTree import ParseError

import inference
from decision import model_decision
from documents import digest, passages, unpack
BASE=Path(__file__).resolve().parent
STATE=Path(os.getenv("EP_REVIEW_STATE",str(BASE/"data")))
TOKEN=secrets.token_urlsafe(32)

@contextmanager
def connect():
    STATE.mkdir(exist_ok=True,parents=True)
    conn=sqlite3.connect(STATE/"policies.sqlite",timeout=15)
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS policies (id TEXT PRIMARY KEY, name TEXT NOT NULL, rules TEXT NOT NULL, model TEXT NOT NULL)")
            yield conn
    finally:
        conn.close()


def saved_catalogue(name, rules):
    entries,controls=[],[]
    for r in rules:
        kind=r.get("kind","legacy")
        row=dict(title=r["title"],clause=r["clause"],rule=r["rule"],refs=r["refs"],
                 category={"ground":r.get("category","Основание"),"method":"Отдельный способ","control":"Условие","legacy":"Ранее извлечённое правило"}[kind],
                 limit=r.get("limit",""),check=r.get("check",""))
        (entries if kind in {"ground","method"} else controls).append(row)
    return dict(name=name,reference=False,entries=entries,controls=controls,
                legacy=any("kind" not in r for r in rules))

def prepare(body):
    files=body.get("files")
    if not isinstance(files,list) or not 1<=len(files)<=8:
        raise ValueError("Загрузите основной текст и при необходимости до семи поправок в порядке их принятия.")
    uploads=[unpack(x) for x in files]
    hashes=[digest(d) for _,d in uploads]
    if len(hashes)!=len(set(hashes)):
        raise ValueError("Один файл добавлен несколько раз.")
    # Amendment order affects the interpretation and belongs in the identity.
    key=digest(("\n".join(hashes)).encode())
    with connect() as db:
        found=db.execute("SELECT name,rules FROM policies WHERE id=?",(key,)).fetchone()
    if found and all("kind" in r and "category" in r for r in json.loads(found[1])):
        return dict(id=key,catalogue=saved_catalogue(found[0],json.loads(found[1])))
    rows=[]
    for i,(name,data) in enumerate(uploads):
        rows.extend(passages(name,data,f"P{i+1}"))
    rules,model=inference.extract_rules(rows)
    label=str(body.get("label") or uploads[0][0]).strip()[:220]
    with connect() as db:
        db.execute("INSERT INTO policies VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET rules=excluded.rules, model=excluded.model",(key,label,json.dumps(rules,ensure_ascii=False),model))
    return dict(id=key,catalogue=saved_catalogue(label,rules))


def review(body):
    with connect() as db:
        item=db.execute("SELECT name,rules FROM policies WHERE id=?",(body.get("policy_id"),)).fetchone()
    if not item:
        raise ValueError("Сначала загрузите Положение.")
    name,raw=unpack(body.get("contract"))
    rules=json.loads(item[1])
    docs=passages(name,raw,"D")
    grounds=[r for r in rules if r["kind"]=="ground"]
    if not grounds:
        raise ValueError("Не удалось извлечь основания ЕП. Сверьте текст Положения.")
    controls=[r for r in rules if r["kind"]=="control"]
    d=model_decision(grounds,docs,controls)
    return {"title":name,"decision":d,"mode":"ИИ по загруженной редакции","meta":item[0],"items":[]}

class Handler(BaseHTTPRequestHandler):
    server_version="EPReview/1"
    def log_message(self,*args):
        pass
    def send_data(self,code,data,kind):
        self.send_response(code)
        self.send_header("Content-Type",kind)
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.end_headers()
        self.wfile.write(data)
    def result(self,code,payload):
        self.send_data(code,json.dumps(payload,ensure_ascii=False).encode(),"application/json; charset=utf-8")
    def local(self):
        host=self.headers.get("Host","")
        return host in {f"127.0.0.1:{self.server.server_port}",f"localhost:{self.server.server_port}"}
    def gateway(self, endpoint):
        if self.headers.get("X-Review-Token")!=TOKEN:
            return self.result(403,{"error":"Обновите страницу локального сервиса."})
        auth=self.headers.get("Authorization","")
        if not auth.startswith("Bearer ") or len(auth)>4096:
            return self.result(401,{"error":"Нужен ключ AgentPlatform."})
        data=None
        if endpoint=="chat/completions":
            try:
                size=int(self.headers.get("Content-Length",0))
            except ValueError:
                size=0
            if not 0<size<=16*1024*1024 or self.headers.get_content_type()!="application/json":
                return self.result(400,{"error":"Нужен JSON-запрос допустимого размера."})
            data=self.rfile.read(size)
        req=urllib.request.Request("https://api.agentplatform.ru/v1/"+endpoint,data=data,headers={"Content-Type":"application/json","Authorization":auth})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args):
                return None
        try:
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=225) as resp:
                self.send_data(200,resp.read(),"application/json; charset=utf-8")
        except urllib.error.HTTPError as e:
            self.result(e.code if 400<=e.code<=599 else 502,{"error":"AgentPlatform отклонил запрос."})
        except (OSError,TimeoutError):
            self.result(502,{"error":"AgentPlatform недоступен."})
    def do_GET(self):
        if not self.local():
            return self.result(403,{"error":"Допускается локальное подключение."})
        path=urlsplit(self.path).path
        if path=="/api/agentplatform/models":
            return self.gateway("models")
        if path in ("/","/index.html"):
            html=(BASE/"index.html").read_text("utf-8").replace('"__TOKEN__"',json.dumps(TOKEN))
            return self.send_data(200,html.encode(),"text/html; charset=utf-8")
        return self.result(404,{"error":"Ресурс не найден."})
    def do_POST(self):
        if not self.local() or self.headers.get("X-Review-Token")!=TOKEN:
            return self.result(403,{"error":"Откройте приложение на локальном адресе и обновите страницу."})
        if self.path=="/api/agentplatform/chat/completions":
            return self.gateway("chat/completions")
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 1<=size<=40*1024*1024:
                return self.result(413,{"error":"Лимит одного запроса — 40 МБ."})
            if self.headers.get_content_type()!="application/json":
                return self.result(415,{"error":"Ожидается JSON."})
            body=json.loads(self.rfile.read(size))
            if not isinstance(body,dict):
                raise ValueError("Некорректное тело запроса.")
            if self.path=="/api/policies":
                payload=prepare(body)
            elif self.path=="/api/review":
                payload=review(body)
            else:
                return self.result(404,{"error":"Ресурс не найден."})
        except (ValueError,binascii.Error,zipfile.BadZipFile,ParseError,KeyError,TypeError,RuntimeError) as e:
            return self.result(422,{"error":str(e)[:500]})
        except Exception:
            return self.result(500,{"error":"Не удалось обработать файл. Проверьте формат документа и подключение к модели."})
        self.result(200,payload)


if __name__=="__main__":
    args=argparse.ArgumentParser()
    args.add_argument("--port",type=int,default=8769)
    args.add_argument("--open",action="store_true")
    config=args.parse_args()
    server=ThreadingHTTPServer(("127.0.0.1",config.port),Handler)
    address=f"http://127.0.0.1:{server.server_port}/"
    print("EP review:",address,flush=True)
    if config.open:
        threading.Timer(0.4,webbrowser.open,args=[address]).start()
    server.serve_forever()
