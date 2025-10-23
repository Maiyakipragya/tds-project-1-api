from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel
import os
import requests
from github import Github, GithubException
import base64
import time

class TaskRequest(BaseModel):
    email: str
    secret: str
    task: str
    round: int
    nonce: str
    brief: str
    checks: list
    evaluation_url: str
    attachments: list

app = FastAPI()

MY_SECRET = os.environ.get("PROJECT_SECRET")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
AI_PIPE_TOKEN = os.environ.get("AI_PIPE_TOKEN")
AI_PIPE_URL = "https://aipipe.org/openai/v1/chat/completions"

github_client = Github(GITHUB_TOKEN)

MIT_LICENSE_TEXT = """
MIT License

Copyright (c) 2025

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

def decode_attachment(attachment):
    try:
        header, encoded = attachment['url'].split(',', 1)
        return base64.b64decode(encoded).decode('utf-8')
    except Exception as e:
        print(f"Error decoding attachment {attachment['name']}: {e}")
        return None

def create_or_update_repo(repo_name: str, files_to_commit: dict, commit_message: str):
    try:
        user = github_client.get_user()
        try:
            repo = user.get_repo(repo_name)
            print(f"Repo '{repo_name}' already exists. Updating files.")
        except GithubException:
            print(f"Creating new public repo: '{repo_name}'")
            repo = user.create_repo(repo_name, private=False, auto_init=True)
            time.sleep(2)

        for file_path, content in files_to_commit.items():
            try:
                file = repo.get_contents(file_path, ref="main")
                repo.update_file(file_path, commit_message, content, file.sha, branch="main")
                print(f"Updated file: {file_path}")
            except GithubException:
                repo.create_file(file_path, commit_message, content, branch="main")
                print(f"Created new file: {file_path}")

        pages_url = f"https://{user.login}.github.io/{repo_name}/"
        pages_endpoint = f"https://api.github.com/repos/{user.login}/{repo_name}/pages"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        data = {"source": {"branch": "main", "path": "/"}}
        response = requests.post(pages_endpoint, json=data, headers=headers)

        if response.status_code == 201:
            print(f"GitHub Pages enabled at: {pages_url}")
        else:
            print(f"GitHub Pages already enabled or error (status {response.status_code}): {response.json().get('message', '')}")

        commit_sha = repo.get_branch("main").commit.sha
        return repo.html_url, pages_url, commit_sha
    except Exception as e:
        print(f"Error in GitHub operation: {e}")
        return None, None, None

def notify_grader(url: str, payload: dict, error_message: str = None):
    if error_message:
        payload["error"] = error_message
    headers = {"Content-Type": "application/json"}
    delay = 1
    for attempt in range(4):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            if response.status_code == 200:
                print(f"Successfully notified grader at {url}")
                return
            else:
                print(f"Grader notification failed (Attempt {attempt + 1}). Status: {response.status_code}. Retrying...")
        except requests.exceptions.RequestException as e:
            print(f"Grader notification failed (Attempt {attempt + 1}). Error: {e}. Retrying...")
        time.sleep(delay)
        delay *= 2
    print(f"Failed to notify grader at {url} after all attempts.")

@app.post("/")
async def handle_task_request(request: TaskRequest, background_tasks: BackgroundTasks):
    if request.secret != MY_SECRET:
        print("Error: Invalid secret received.")
        return {"status": "error", "message": "Invalid secret"}
    print(f"Received valid request for task: {request.task} (Round: {request.round})")
    background_tasks.add_task(process_task_in_background, request)
    return {"status": "Request received. Processing in background."}

def process_task_in_background(request: TaskRequest):
    print(f"--- Starting background job for task: {request.task} ---")
    notification_payload = {
        "email": request.email,
        "task": request.task,
        "round": request.round,
        "nonce": request.nonce
    }
    generated_code = None
    try:
        print("Generating code with AI Pipe...")
        attachment_info = "\n".join([f"File: {a['name']}" for a in request.attachments])
        prompt_content = f"""
        You are an expert web developer. Your task is to generate a single, self-contained HTML file named 'index.html' based on the following brief.
        The final output must be ONLY the raw HTML code, with no explanations, comments, or markdown.
        **Brief:** {request.brief}
        **Attachments:** Your code should fetch files like {attachment_info} from the same directory (e.g., './data.csv').
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AI_PIPE_TOKEN}"
        }
        data = {
            "model": "gpt-3.5-turbo",
            "messages": [
                {"role": "system", "content": "You are an expert web developer who returns only raw HTML code for 'index.html'."},
                {"role": "user", "content": prompt_content}
            ]
        }
        response = requests.post(AI_PIPE_URL, headers=headers, json=data)
        if response.status_code != 200:
            raise Exception(f"AI Pipe API Error: {response.status_code} - {response.text}")
        generated_code = response.json()['choices'][0]['message']['content'].strip()
        if generated_code.startswith("```html"):
            generated_code = generated_code[7:]
        if generated_code.endswith("```"):
            generated_code = generated_code[:-3]
        print("Successfully generated code from AI Pipe.")
    except Exception as e:
        print(f"An error occurred during LLM code generation: {e}")
        notify_grader(request.evaluation_url, notification_payload, error_message=f"LLM generation failed: {e}")
        return
    try:
        repo_name = request.task
        commit_message = f"Round {request.round}: {request.brief}"
        readme_content = f"""
        # Project: {repo_name}

        ## Summary
        This project was auto-generated for the TDS Project 1 in response to the brief:
        "{request.brief}"

        ## Usage
        This is a static site. The deployed version is available via GitHub Pages.

        ## Code Explanation
        The `index.html` file is a self-contained application generated by an LLM.
        Any attachments, like `data.csv`, are fetched by the HTML file.

        ## License
        This project is licensed under the MIT License.
        """
        files_to_commit = {
            "index.html": generated_code,
            "README.md": readme_content,
            "LICENSE": MIT_LICENSE_TEXT
        }
        for attachment in request.attachments:
            content = decode_attachment(attachment)
            if content:
                files_to_commit[attachment['name']] = content
                print(f"Added attachment: {attachment['name']}")
        print(f"Pushing files to GitHub repo: {repo_name}")
        repo_url, pages_url, commit_sha = create_or_update_repo(repo_name, files_to_commit, commit_message)
        if not repo_url:
            raise Exception("Failed to create or update GitHub repository.")
        print(f"Successfully deployed to GitHub. Repo: {repo_url}, Pages: {pages_url}")
        notification_payload.update({
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "pages_url": pages_url
        })
        notify_grader(request.evaluation_url, notification_payload)
    except Exception as e:
        print(f"An error occurred during GitHub deployment: {e}")
        notify_grader(request.evaluation_url, notification_payload, error_message=f"Deployment failed: {e}")
    print(f"--- Finished background job for task: {request.task} ---")
