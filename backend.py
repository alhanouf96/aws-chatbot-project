from fastapi import FastAPI, File, UploadFile, HTTPException, Depends
from pydantic import BaseModel
from openai import OpenAI
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
import json
import psycopg2
import os
import uuid
from psycopg2.extras import RealDictCursor
from typing import List, Optional
from langchain_community.document_loaders import PyPDFLoader
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_chroma import Chroma
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.chains import create_history_aware_retriever, create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.messages import HumanMessage, AIMessage
import chromadb
import boto3
from io import BytesIO
from botocore.exceptions import ClientError

# تحميل env
load_dotenv()

app = FastAPI()

def get_aws_secrets(secret_name, region_name="us-east-1"):
    client = boto3.client("secretsmanager", region_name=region_name)

    try:
        response = client.get_secret_value(SecretId=secret_name)
    except ClientError as e:
        raise e

    secret_dict = json.loads(response['SecretString'])
    return secret_dict


# استخدام السر
secret_name = os.environ.get("SECRET_NAME", "hanoufsecrets")
secrets = get_aws_secrets(secret_name)

DB_NAME = secrets['PROJ-DB-NAME']
DB_USER = secrets['PROJ-DB-USER']
DB_PASSWORD = secrets['PROJ-DB-PASSWORD']
DB_HOST = secrets['PROJ-DB-HOST']
DB_PORT = secrets['PROJ-DB-PORT']
OPENAI_API_KEY = secrets['PROJ-OPENAI-API-KEY']
AWS_ACCESS_KEY_ID = secrets['PROJ-AWS-ACCESS-KEY-ID']
AWS_SECRET_ACCESS_KEY = secrets['PROJ-AWS-SECRET-ACCESS-KEY']
AWS_STORAGE_BUCKET_NAME = secrets['PROJ-AWS-STORAGE-BUCKET-NAME']
AWS_REGION = secrets['PROJ-AWS-REGION']

print("OpenAI API KEY:", OPENAI_API_KEY)

DB_CONFIG = {
    "dbname": DB_NAME,
    "user": DB_USER,
    "password": DB_PASSWORD,
    "host": DB_HOST,
    "port": DB_PORT,
}
OPENAI_API_KEY = secrets['PROJ-OPENAI-API-KEY']

# نفس أسلوبك القديم:
client = OpenAI(api_key=OPENAI_API_KEY)

model = "gpt-3.5-turbo"

llm = ChatOpenAI(model=model, api_key=OPENAI_API_KEY)

# LangChain setup
embedding_function = OpenAIEmbeddings(api_key=OPENAI_API_KEY)
chroma_client = chromadb.HttpClient(host='localhost', port=8000)
vectorstore = Chroma(client=chroma_client, collection_name="langchain", embedding_function=embedding_function)

# S3 config
s3 = boto3.client('s3')
S3_BUCKET = AWS_STORAGE_BUCKET_NAME



# Models
class ChatRequest(BaseModel):
    messages: List[dict]

class SaveChatRequest(BaseModel):
    chat_id: str
    chat_name: str
    messages: List[dict]
    pdf_name: Optional[str] = None
    pdf_path: Optional[str] = None
    pdf_uuid: Optional[str] = None

class DeleteChatRequest(BaseModel):
    chat_id: str

class RAGChatRequest(BaseModel):
    messages: List[dict]
    pdf_uuid: str

# DB Dependency
def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()

@app.post("/chat/")
async def chat(request: ChatRequest):
    try:
        stream = client.chat.completions.create(
            model=model,
            messages=request.messages,
            stream=True,
        )

        def stream_response():
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta

        return StreamingResponse(stream_response(), media_type="text/plain")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/load_chat/")
async def load_chat(db: psycopg2.extensions.connection = Depends(get_db)):
    try:
        with db.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                "SELECT id, name, file_path, pdf_name, pdf_path, pdf_uuid FROM advanced_chats ORDER BY last_update DESC"
            )
            rows = cursor.fetchall()

        records = []
        print(f"📋 Trying to load chat records from DB... Using Bucket: {S3_BUCKET}")
        for row in rows:
            try:
                obj = s3.get_object(Bucket=S3_BUCKET, Key=row["file_path"])
                blob_data = obj['Body'].read()

                if not blob_data:
                    print(f"⚠️ Empty file at {row['file_path']}, skipping this record.")
                    continue

                try:
                    messages = json.loads(blob_data)
                except json.JSONDecodeError as e:
                    print(f"❌ JSON decode error in file {row['file_path']}: {e}")
                    continue

                records.append({
                    "id": row["id"],
                    "chat_name": row["name"],
                    "messages": messages,
                    "pdf_name": row["pdf_name"],
                    "pdf_path": row["pdf_path"],
                    "pdf_uuid": row["pdf_uuid"],
                })

            except s3.exceptions.NoSuchKey:
                print(f"⚠️ File not found in S3: {row['file_path']}, skipping this record.")
                continue
            except Exception as e:
                print(f"❌ Unexpected error while loading file {row['file_path']}: {e}")
                continue

        return records

    except Exception as e:
        print(f"❌ General error in /load_chat/: {e}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@app.post("/save_chat/")
async def save_chat(request: SaveChatRequest, db: psycopg2.extensions.connection = Depends(get_db)):
    try:
        file_path = f"chat_logs/{request.chat_id}.json"
        messages_data = json.dumps(request.messages, ensure_ascii=False, indent=4)

        s3.upload_fileobj(BytesIO(messages_data.encode("utf-8")), S3_BUCKET, file_path)

        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO advanced_chats (id, name, file_path, last_update, pdf_path, pdf_name, pdf_uuid)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP, %s, %s, %s)
                ON CONFLICT (id)
                DO UPDATE SET name = EXCLUDED.name, file_path = EXCLUDED.file_path, last_update = CURRENT_TIMESTAMP, pdf_path = EXCLUDED.pdf_path, pdf_name = EXCLUDED.pdf_name, pdf_uuid = EXCLUDED.pdf_uuid
                """,
                (request.chat_id, request.chat_name, file_path, request.pdf_path, request.pdf_name, request.pdf_uuid)
            )
        db.commit()
        return {"message": "Chat saved successfully"}

    except Exception as e:
        db.rollback()
        print(f"❌ Error in save_chat: {e}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.post("/delete_chat/")
async def delete_chat(request: DeleteChatRequest, db: psycopg2.extensions.connection = Depends(get_db)):
    try:
        file_path = None
        with db.cursor() as cursor:
            cursor.execute("SELECT file_path, pdf_path FROM advanced_chats WHERE id = %s", (request.chat_id,))
            result = cursor.fetchone()
            if result:
                file_path, pdf_path = result[0], result[1]
            else:
                raise HTTPException(status_code=404, detail="Chat not found")

        with db.cursor() as cursor:
            cursor.execute("DELETE FROM advanced_chats WHERE id = %s", (request.chat_id,))
        db.commit()

        if file_path:
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=file_path)
            except s3.exceptions.NoSuchKey:
                pass

        if pdf_path:
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=pdf_path)
            except s3.exceptions.NoSuchKey:
                pass

        return {"message": "Chat deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@app.post("/upload_pdf/")
async def upload_pdf(file: UploadFile = File(...)):
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are allowed.")

    try:
        pdf_uuid = str(uuid.uuid4())
        file_path = f"pdf_store/{pdf_uuid}_{file.filename}"
        os.makedirs("pdf_store", exist_ok=True)

        with open(file_path, "wb") as f:
            f.write(await file.read())

        with open(file_path, "rb") as f:
            s3.upload_fileobj(f, S3_BUCKET, file_path)

        loader = PyPDFLoader(file_path)
        documents = loader.load()
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
        texts = text_splitter.split_documents(documents)

        vectorstore.add_texts(
            [doc.page_content for doc in texts],
            ids=[str(uuid.uuid4()) for _ in texts],
            metadatas=[{"pdf_uuid": pdf_uuid} for _ in texts],
        )

        os.remove(file_path)

        return {"message": "File uploaded successfully", "pdf_path": file_path, "pdf_uuid": pdf_uuid}

    except Exception as e:
        print(f"❌ Error in upload_pdf: {e}")
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")


@app.post("/rag_chat/")
async def rag_chat(request: RAGChatRequest):
    retriever = vectorstore.as_retriever(search_kwargs={"k": 5, "filter": {"pdf_uuid": request.pdf_uuid}})

    contextualize_q_system_prompt = (
        "Given a chat history and the latest user question "
        "which might reference context in the chat history, "
        "formulate a standalone question which can be understood "
        "without the chat history. Do NOT answer the question, "
        "just reformulate it if needed and otherwise return it as is."
    )
    contextualize_q_prompt = ChatPromptTemplate.from_messages([
        ("system", contextualize_q_system_prompt),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
    ])
    history_aware_retriever = create_history_aware_retriever(llm, retriever, contextualize_q_prompt)

    system_prompt = (
        "You are an assistant for question-answering tasks. "
        "Use the following pieces of retrieved context to answer "
        "the question. If you don't know the answer, say that you "
        "don't know. Use three sentences maximum and keep the "
        "answer concise."
        "\n\n"
        "{context}"
    )
    qa_prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
    ])
    question_answer_chain = create_stuff_documents_chain(llm, qa_prompt)
    rag_chain = create_retrieval_chain(history_aware_retriever, question_answer_chain)

    chat_history = []
    for message in request.messages:
        if message["role"] == "user":
            chat_history.append(HumanMessage(content=message["content"]))
        if message["role"] == "assistant":
            chat_history.append(AIMessage(content=message["content"]))

    chain = rag_chain.pick("answer")
    stream = chain.stream({"chat_history": chat_history, "input": request.messages[-1]})

    def stream_response():
        for chunk in stream:
            yield chunk

    return StreamingResponse(stream_response(), media_type="text/plain")




