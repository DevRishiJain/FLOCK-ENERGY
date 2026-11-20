import json

from urja_api.api import app

json.dump(app.openapi(), open("openapi.json", "w"), indent=2)
print("wrote openapi.json")
