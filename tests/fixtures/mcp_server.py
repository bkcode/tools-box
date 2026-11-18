"""Only a local read-only protocol fixture. No real deployment tools."""
import sys
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP("tools-box-test", stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

@mcp.tool()
def list_jobs() -> dict:
    return {"workspace": "fixture", "environment": "dev", "jobs": []}

@mcp.tool()
def list_services() -> dict:
    return {"workspace": "fixture", "environment": "dev", "services": ["fixture-service"]}

if __name__ == "__main__":
    if len(sys.argv) == 2:
        import uvicorn
        from starlette.responses import JSONResponse
        app = mcp.streamable_http_app()
        async def authenticated(scope, receive, send):
            if scope["type"] == "http" and dict(scope.get("headers", [])).get(b"authorization") != b"Bearer fixture-only":
                await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
                return
            await app(scope, receive, send)
        uvicorn.run(authenticated, host="127.0.0.1", port=int(sys.argv[1]), access_log=False)
    else:
        mcp.run(transport="stdio")
