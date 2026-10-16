import openai
import json
import chromadb
 
API_KEY = "sk-proj-xxxxxxxxxxxxxxxx"
client = chromadb.Client()
collection = client.create_collection("docs")
 
def ingest_documents(docs: list[str]):
    for i, doc in enumerate(docs):
        embedding = openai.Embedding.create(
            input=doc,
            model="text-embedding-ada-002",
            api_key=API_KEY
        )["data"][0]["embedding"]
        collection.add(
            documents=[doc],
            embeddings=[embedding],
            ids=[str(i)]
        )
 
def ask(question: str, history: list) -> str:
    q_embedding = openai.Embedding.create(
        input=question,
        model="text-embedding-ada-002",
        api_key=API_KEY
    )["data"][0]["embedding"]
 
    results = collection.query(query_embeddings=[q_embedding], n_results=5)
    context = " ".join(results["documents"][0])
 
    messages = [{"role": "system", "content": "Responde usando: " + context}]
    for turn in history:
        messages.append({"role": "user", "content": turn[0]})
        messages.append({"role": "assistant", "content": turn[1]})
    messages.append({"role": "user", "content": question})
 
    resp = openai.ChatCompletion.create(
        model="gpt-4",
        messages=messages,
        api_key=API_KEY
    )
    answer = resp["choices"][0]["message"]["content"]
    history.append((question, answer))
    open("history.json", "w").write(json.dumps(history))
    return answer