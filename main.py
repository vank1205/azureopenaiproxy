import os, json, asyncio, httpx, websockets
from itertools import cycle
from fastapi import FastAPI, Request, Response, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

AZURE_KEY = os.getenv("AZURE_OPENAI_API_KEY")
PROXY_KEY = os.getenv("PROXY_API_KEY")

AZ_EP = {
    "gpt-4-04-14-uplyf-1":
        "https://uplyf-ai-foundry.openai.azure.com/openai/deployments/"
        "gpt-4-04-14-uplyf-1/chat/completions?api-version=2025-01-01-preview",
    "gpt-4-04-14-uplyf-2":
        "https://uplyf-ai-foundry.openai.azure.com/openai/deployments/"
        "gpt-4-04-14-uplyf-2/chat/completions?api-version=2025-01-01-preview",
    "model-router-uplyf":
        "https://uplyf-ai-foundry.openai.azure.com/openai/deployments/"
        "model-router-uplyf/chat/completions?api-version=2025-01-01-preview",
    "whisper-1":
        "https://uplyf-ai-foundry.openai.azure.com/openai/deployments/"
        "whisper-1/audio/transcriptions?api-version=2025-01-01-preview",
    "tts-gpt4o":
        "https://uplyf-ai-foundry.openai.azure.com/openai/deployments/"
        "gpt-4o-mini-realtime-preview/audio/speech?api-version=2025-01-01-preview",
    "realtime-gpt4o":
        "https://uplyf-ai-foundry.cognitiveservices.azure.com/openai/realtime"
        "?api-version=2024-10-01-preview&deployment=gpt-4o-mini-realtime-preview",
}

UPLYF_PAIR = ["gpt-4-04-14-uplyf-1", "gpt-4-04-14-uplyf-2"]
ROUND_ROBIN = cycle(UPLYF_PAIR)

app = FastAPI()

def header_key(headers) -> str | None:
    auth = headers.get("authorization", "")
    return auth.split(" ", 1)[1] if auth.startswith("Bearer ") else None

def http_auth(req: Request):
    if header_key(req.headers) != PROXY_KEY:
        raise HTTPException(401, "Invalid or missing proxy key")

async def forward(url, *, data=None, files=None, hdr=None):
    h = {"api-key": AZURE_KEY}
    if hdr: h.update(hdr)
    async with httpx.AsyncClient(timeout=60) as c:
        return await c.post(url, headers=h,
                            content=data if files is None else None,
                            files=files)

def j(obj, code=200):
    return Response(json.dumps(obj, ensure_ascii=False), code,
                    media_type="application/json")

@app.post("/v1/chat/completions")
async def chat(req: Request):
    http_auth(req)
    raw = await req.body()
    body = json.loads(raw)
    model = body.get("model")

    if model in ["gpt-4-04-14-uplyf", *UPLYF_PAIR]:
        for _ in range(len(UPLYF_PAIR)):
            tgt = next(ROUND_ROBIN)
            body["model"] = tgt
            resp = await forward(AZ_EP[tgt],
                                 data=json.dumps(body).encode(),
                                 hdr={"Content-Type":"application/json"})
            if resp.status_code == 429:
                await asyncio.sleep(0.2); continue
            if resp.status_code == 400 and b"content management policy" in resp.content:
                return j({"choices":[{"message":{"role":"assistant",
                        "content":"I’m sorry, I can’t answer that. Please rephrase."},
                        "finish_reason":"stop","index":0}],
                          "object":"chat.completion"})
            return Response(resp.content, resp.status_code,
                            media_type=resp.headers.get("content-type"))
        return j({"choices":[{"message":{"role":"assistant",
                "content":"Service busy, try again shortly."},
                "finish_reason":"stop","index":0}],
                  "object":"chat.completion"})

    if model == "model-router-uplyf":
        resp = await forward(AZ_EP[model], data=raw,
                             hdr={"Content-Type":"application/json"})
        return Response(resp.content, resp.status_code,
                        media_type=resp.headers.get("content-type"))

    raise HTTPException(400, "Unsupported model on this endpoint")

@app.post("/v1/audio/transcriptions")
async def stt(req: Request):
    http_auth(req)
    form = await req.form()
    files = {"file": (form["file"].filename, form["file"].file,
                      form["file"].content_type)}
    other = {k:v for k,v in form.items() if k!="file"}
    resp = await forward(AZ_EP["whisper-1"], files={**files, **other})
    return Response(resp.content, resp.status_code,
                    media_type=resp.headers.get("content-type"))

@app.post("/v1/audio/speech")
async def tts(req: Request):
    http_auth(req)
    data = await req.body()
    resp = await forward(AZ_EP["tts-gpt4o"], data=data,
                         hdr={"Content-Type":"application/json"})
    return Response(resp.content, resp.status_code,
                    media_type=resp.headers.get("content-type"))

@app.options("/v1/realtime/sessions")
async def realtime_options():
    return PlainTextResponse("ok", 200, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Allow-Methods": "OPTIONS, GET, POST"
    })

@app.websocket("/v1/realtime/sessions")
async def realtime_ws(client: WebSocket):
    # Accept proxy key via header **or** ?key= query parameter
    key = header_key(client.headers) or client.query_params.get("key")
    if key != PROXY_KEY:
        await client.close(code=4403); return
    await client.accept()

    # Connect to Azure (Cognitive Services) WebSocket
    az_url = AZ_EP["realtime-gpt4o"].replace("https://", "wss://")
    async with websockets.connect(az_url,
                                  extra_headers={"api-key": AZURE_KEY},
                                  max_size=1<<25) as az:

        async def to_az():
            try:
                while True:
                    msg = await client.receive_text()
                    await az.send(msg)
            except WebSocketDisconnect:
                await az.close()
            except websockets.ConnectionClosed:
                await client.close()

        async def to_client():
            try:
                async for msg in az:
                    await client.send_text(msg)
            except websockets.ConnectionClosed:
                await client.close()

        await asyncio.gather(to_az(), to_client)
