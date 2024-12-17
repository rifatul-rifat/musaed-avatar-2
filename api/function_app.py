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

def get_product_information(user_question, categories='*', top_k=1):
    """ Vectorize user query to search Cognitive Search vector search on index_name. Optional filter on categories field. """
     
    url = f"{search_endpoint}/indexes/{search_index_name}/docs/search?api-version={search_api_version}"

    headers = {
        "Content-Type": "application/json",
        "api-key": f"{search_key}",
    }
    
    vector = generate_embeddings(user_question)

    data = {
        "vectors": [
            {
                "value": vector,
                "fields": "description_vector",
                "k": top_k
            },
        ],
        "select": "tagline, description, original_price, special_offer, product_image_file",
    }

    # optional filtered search
    if categories != '*':
        data["filter"] = f"category eq '{categories}'"

    results = requests.post(url, headers=headers, data=json.dumps(data))    
    results_json = results.json()

    print('results_json', results_json)
    
    # Extracting the required fields from the results JSON
    product_data = results_json['value'][0] # hard limit to top result for now

    response_data = {
        "tagline": product_data.get('tagline'),
        "description": product_data.get('description'),
        "original_price": product_data.get('original_price'),
        "special_offer": product_data.get('special_offer'),
        "product_image_file": product_data.get('product_image_file'),
    }
    return json.dumps(response_data)

def display_product_info(product_info, display_size=40):
    """ Display product information """

    # Show image
    image_file = product_info['product_image_file']

    image_url = blob_sas_url.split("?")[0] + f"/{image_file}?" + blob_sas_url.split("?")[1]

    response = requests.get(image_url)
    print(image_url)

    # Check if the request was successful
    if response.status_code == 200:
        return {
            "tagline": product_info['tagline'],
            "original_price": product_info['original_price'],
            "special_offer": product_info['special_offer'],
            "image_url": image_url 
            }
    else:
        print(f"Failed to retrieve image. HTTP Status code: {response.status_code}")

    print(f"""
    {product_info['tagline']}
    Original price: ${product_info['original_price']} Special offer: ${product_info['special_offer']} 
    """)

def generate_embeddings(text):
    """ Generate embeddings for an input string using embeddings API """

    url = f"{endpoint}/openai/deployments/{embeddings_deployment}/embeddings?api-version=2023-09-01-preview"

    headers = {
        "Content-Type": "application/json",
        "api-key": api_key,
    }

    data = {"input": text}

    response = requests.post(url, headers=headers, data=json.dumps(data)).json()
    return response['data'][0]['embedding']

def remove_html_tags(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    return soup.get_text()

def bing_web_search(search_term):
    """Searches for news and webpages using the Bing Search API and returns matches in a string. Uses sinippets from search engine only. No scraping of web sites."""
    logging.info(f'Searching for: {search_term}')

    # bing search request
    headers = {"Ocp-Apim-Subscription-Key": bing_key}
    params = {"q": search_term, "textDecorations": True, "textFormat": "HTML", "count" : 5,}
    response = requests.get(search_url, headers=headers, params=params)
    response.raise_for_status()
    search_results = response.json()

    # consolidate news and webpage hits into string
    results_str = f"Here are the web search search results for the user query: {search_term}\nThe search engine returned news and links to websites."

    # Parsing news
    if 'news' in search_results:
        results_str += "\n*** News: ***"
        news = search_results['news']['value']

        for index, result in enumerate(news):
            news_str = f"""
        News {index + 1}/{len(news)}:
        Title: {remove_html_tags(result.get('name', 'No title available'))}
        Description: {remove_html_tags(result.get('description', 'No snippet available'))}
        Provider: {result['provider'][0].get('name', 'No provider name available')}
        URL: {result.get('url', 'No URL available')}
        """
            results_str += news_str

    # Parsing webpage hits
    results_str += "\n*** Web pages:***"
    webpages = search_results['webPages']['value']

    for index, result in enumerate(webpages):
        news_str = f"""
    Webpage {index + 1}/{len(webpages)}:
    Title: {result.get('name', 'No title available')}
    Snippet: {remove_html_tags(result.get('snippet', 'No snippet available'))}
    Site name: {result.get('siteName', 'No site name available')}
    URL: {result.get('url', 'No URL available')}
    """
        results_str += news_str

    return results_str



def get_bonus_points(account_id):
    """Retrieve bonus points and its cash value for a given account ID."""
     
    # Define the SQL query to retrieve loyalty_points for the given account_id
    query = "SELECT loyalty_points FROM Customers WHERE account_id = ?"

    # Execute the query with account_id as a parameter
    results = execute_sql_query(query, params=(account_id,))

    # If results are empty, return an error message in JSON format
    if not results:
        return json.dumps({"error": "Account not found"})

    # Get the loyalty_points value
    loyalty_points = results[0][0]

    # Convert loyalty_points to cash_value
    cash_value = loyalty_points / 9.5

    # Create a JSON object with the required keys and values
    response_json = json.dumps({
        "available_bonus_points": loyalty_points,
        "cash_value": cash_value
    })

    return response_json


def get_order_details(account_id):
     
    # Get orders and corresponding product names for the account_id
    query = '''
        SELECT o.order_id, p.name as product_name, o.days_to_delivery
        FROM Orders o
        JOIN Products p ON o.product_id = p.id
        WHERE o.account_id = ?
    '''
    orders = execute_sql_query(query, params=(account_id,))
    
    # Get today's date and calculate the expected delivery date for each order
    today = datetime.today()
    
    # Create a JSON object with the required details
    order_details = [
        {
            "product_name": order.product_name,
            "expected_delivery_date": (today + timedelta(days=order.days_to_delivery)).strftime('%Y-%m-%d')
        }
        for order in orders
    ]
    
    # Return the JSON object
    return json.dumps(order_details)

def order_product(account_id, product_name, quantity=1):
     
    # Step 1: Find the maximum existing order_id
    query = "SELECT MAX(order_id) FROM Orders"
    results = execute_sql_query(query)
    max_order_id = results[0][0] if results[0][0] is not None else 0

    # Step 2 & 3: Find product ID and check stock
    query = "SELECT id, name, stock FROM Products WHERE LOWER(name) LIKE LOWER(?)"
    params = (f'%{product_name}%',)
    results = execute_sql_query(query, params=params)
    
    # Handling no match found
    if not results:
        return json.dumps({"info": "No matching product found"})
    
    product_id, product_name_corrected, stock = results[0]
    
    # Check if the stock is sufficient
    if stock < quantity:
        return json.dumps({"info": "Insufficient stock"})
    
    # Step 4: Place the order
    # Deducting the ordered quantity from the stock
    query = "UPDATE Products SET stock = stock - ? WHERE id = ?"
    params = (quantity, product_id)
    if place_orders: execute_sql_query(query, params=params)

    # Adding the order details to the Orders table
    days_to_delivery = 5
    for i in range(quantity):
        max_order_id += 1
        query = "INSERT INTO Orders (order_id, product_id, days_to_delivery, account_id) VALUES (?, ?, ?, ?)"
        params = (max_order_id, product_id, days_to_delivery, account_id)
        if place_orders: execute_sql_query(query, params=params)
    
    # Step 5: Calculate the expected delivery date and return the JSON object
    today = datetime.now()
    expected_delivery_date = today + timedelta(days=days_to_delivery)
    
    return json.dumps({
        "info": "Order placed",
        "product_name": product_name_corrected,
        "expected_delivery_date": expected_delivery_date.strftime('%Y-%m-%d')
    })


def execute_sql_query(query, connection_string=database_connection_string, params=None):
    """Execute a SQL query and return the results."""
    results = []
    print('database_connection_string', database_connection_string)
    
    # Establish the connection
    with pyodbc.connect(connection_string) as conn:
        cursor = conn.cursor()
        
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        
        # If the query is a SELECT statement, fetch results
        if query.strip().upper().startswith('SELECT'):
            results = cursor.fetchall()
        
        conn.commit()

    return results


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
