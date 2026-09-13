def handle(request):
    return {"hits": [{"title": request["payload"].get("query", "")}]} 
