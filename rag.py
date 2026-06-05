import os
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_ollama import OllamaLLM
from langchain_core.prompts import ChatPromptTemplate

FILE_PATH = r"C:\Users\Bhanu\Downloads\custom_balance_sheet_20_pages.pdf"
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50

def load_document(file_path):
    if file_path.endswith(".txt"):
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()

    elif file_path.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        text = ""
        for page in reader.pages:
            if page.extract_text():
                text += page.extract_text()
        return text

    else:
        raise ValueError("Unsupported file type")


def chunk_text(text):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP
    )
    return splitter.create_documents([text])


embedding_model = HuggingFaceEmbeddings(
    model_name="sentence-transformers/all-MiniLM-L6-v2"
)


def create_vectorstore(docs):
    return FAISS.from_documents(docs, embedding_model)


llm = OllamaLLM(model="qwen3:8b", temperature=0.2)


prompt = ChatPromptTemplate.from_template("""
You are a financial assistant.

If the question requires calculation (like total, sum, highest):
- Carefully extract numbers from context
- Perform the calculation step-by-step

If answer not found, say "Not found in document".

Context:
{context}

Question:
{question}
""")


def ask_question(query, vectorstore):
    retriever = vectorstore.as_retriever(search_kwargs={"k": 4})

    docs = retriever.invoke(query)   

    context = "\n\n".join([doc.page_content for doc in docs])

    final_prompt = prompt.format(context=context, question=query)

    response = llm.invoke(final_prompt)
    return response


def main():
    print("Loading document...")
    text = load_document(FILE_PATH)

    print("Chunking...")
    docs = chunk_text(text)

    print("Creating embeddings + vector DB...")
    vectorstore = create_vectorstore(docs)

    print("\n Ready! Ask questions (type 'exit' to quit)\n")

    while True:
        query = input(" Your question: ") 

        if query.lower() == "exit":
            break

        response = ask_question(query, vectorstore)
        print("\n Answer:", response, "\n")


if __name__ == "__main__":
    main()