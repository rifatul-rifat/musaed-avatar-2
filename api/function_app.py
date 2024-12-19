import azure.functions as func
import openai
from azurefunctions.extensions.http.fastapi import Request, StreamingResponse, JSONResponse
import asyncio
import os
import logging
import pyodbc
import requests
import json
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import aiohttp

# Azure Function App
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
api_key = os.environ["AZURE_OPENAI_API_KEY"]
subscription_key = os.getenv("AZURE_SPEECH_API_KEY")
region = os.getenv("AZURE_SPEECH_REGION")
search_endpoint = os.getenv("AZURE_SEARCH_ENDPOINT")
search_key = os.getenv("AZURE_SEARCH_API_KEY") 
search_api_version = '2023-07-01-Preview'
search_index_name = os.getenv("AZURE_SEARCH_INDEX")
bing_key = os.getenv("BING_KEY")
search_url = os.getenv("BING_SEARCH_URL")
blob_sas_url = os.getenv("BLOB_SAS_URL")
place_orders = False

sql_db_server = os.getenv("SQL_DB_SERVER")
sql_db_user = os.getenv("SQL_DB_USER")
sql_db_password = os.getenv("SQL_DB_PASSWORD")
sql_db_name = os.getenv("SQL_DB_NAME")
server_connection_string = f"Driver={{ODBC Driver 17 for SQL Server}};Server=tcp:{sql_db_server},1433;Uid={sql_db_user};Pwd={sql_db_password};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
database_connection_string = server_connection_string + f"Database={sql_db_name};"

# Azure Open AI
deployment = os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"]
embeddings_deployment = os.getenv("AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT")

temperature = 0.7

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_basamh_information",
            "description": "This function have access to all of Basamh company's internal documents and can provide information to basamh employees.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_question": {
                        "type": "string",
                        "description": "User question (i.e., How is the budget for business trip?, etc.)"
                    },
                },
                "required": ["user_question"],
            }
        }
    }
]


client = openai.AsyncAzureOpenAI(
    azure_endpoint=endpoint,
    api_key=api_key,
    api_version="2023-09-01-preview"
)

async def get_basamh_information(user_question, chat_history):
    """ Asynchronous generator to stream data from the API. """
    url = "https://musaed-backend.azurewebsites.net/get-oai-response/"
    
    payload = {
        "question": user_question,
        "chat_history": chat_history
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            async for line in response.content:
                if res_chunk := line.decode("utf-8").strip():  # Ensure the line is not empty
                    try:
                        parsed_chunk = json.loads(res_chunk)
                        if answer_chunk := parsed_chunk.get("answer_chunk"):
                            yield answer_chunk
                    except json.JSONDecodeError:
                        print(f"Non-JSON response: {res_chunk}")



# Get data from Azure Open AI
async def stream_processor(response, messages):

    func_call = {
                  "id": None,
                  "type": "function",
                  "function": {
                        "name": None,
                        "arguments": ""
                  }
                  }

    async for chunk in response:
        if len(chunk.choices) > 0:
            delta = chunk.choices[0].delta

            if delta.content is None:
                if delta.tool_calls:
                    tool_calls = delta.tool_calls
                    tool_call = tool_calls[0]
                    if tool_call.id != None:
                        func_call["id"] = tool_call.id
                    if tool_call.function.name != None:
                        func_call["function"]["name"] = tool_call.function.name
                    if tool_call.function.arguments != None:
                        func_call["function"]["arguments"] += tool_call.function.arguments
                        await asyncio.sleep(0.01)
                        try:
                            arguments = json.loads(func_call["function"]["arguments"])
                            print(f"Function generation requested, calling function", func_call)
                            # messages.append({
                            #     "content": None,
                            #     "role": "assistant",
                            #     "tool_calls": [func_call]
                            # })

                            available_functions = {
                                "get_basamh_information": get_basamh_information,
                                # "bing_web_search": bing_web_search,
                                # "get_bonus_points": get_bonus_points,
                                # "get_order_details": get_order_details,
                                # "order_product": order_product
                            }
                            function_to_call = available_functions[func_call["function"]["name"]] 
                            # function_response = function_to_call(**arguments)

                            # response_chunks = []
                            if function_to_call == get_basamh_information:
                                arguments.update({"chat_history": messages})
                                async for chunk in get_basamh_information(**arguments):
                                    yield chunk
                                # response_chunks.append(chunk)

                            # function_response = "".join(response_chunks)


                            # messages.append({
                            #     "role": "assistant",
                            #     "content": function_response
                            # })


                        except Exception as e:
                            print(e)

            if delta.content: # Get remaining generated response if applicable
                await asyncio.sleep(0.01)
                yield delta.content


# HTTP streaming Azure Function
@app.route(route="get-oai-response", methods=[func.HttpMethod.GET, func.HttpMethod.POST])
async def stream_openai_text(req: Request) -> StreamingResponse:

    body = await req.body()

    messages_obj = json.loads(body) if body else []
    messages = messages_obj['messages']

    azure_open_ai_response = await client.chat.completions.create(
        model=deployment,
        temperature=temperature,
        max_tokens=1000,
        messages=messages,
        tools=tools,
        stream=True
    )

    return StreamingResponse(stream_processor(azure_open_ai_response, messages), media_type="text/event-stream")

@app.route(route="get-ice-server-token", methods=[func.HttpMethod.GET, func.HttpMethod.POST])
def get_ice_server_token(req: Request) -> JSONResponse:
    logging.info('Python HTTP trigger function processed a request.')

    # Define token endpoint
    token_endpoint = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/avatar/relay/token/v1"

    # Make HTTP request with subscription key as header
    response = requests.get(token_endpoint, headers={"Ocp-Apim-Subscription-Key": subscription_key})

    if response.status_code == 200:
        return JSONResponse(
            content = response.json(),
            status_code=200,
            headers={"Content-Type": "application/json"}
        )
    else:
        return func.HttpResponse(f"Error {response.status_code}: {response.text}")
    

@app.route(route="get-speech-token", methods=[func.HttpMethod.GET, func.HttpMethod.POST])
def get_speech_token(req: Request) -> JSONResponse:
    logging.info('Python HTTP trigger function processed a request.')

    # Define token endpoint
    token_endpoint = f"https://{region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"

    # Make HTTP request with subscription key as header
    response = requests.post(token_endpoint, headers={"Ocp-Apim-Subscription-Key": subscription_key})

    print(response)

    if response.status_code == 200:
        return JSONResponse(
            content = {"token": response.text},
            status_code=200,
            headers={"Content-Type": "application/json"}
        )
    else:
        return func.HttpResponse(f"Error {response.status_code}: {response.text}")
