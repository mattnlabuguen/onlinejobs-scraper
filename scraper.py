import json
import os
from typing import Callable

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# Global session for shared state
_global_session = None

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "upgrade-insecure-requests": "1",
}


def create_session() -> requests.Session | None:
    """Create a new authenticated session."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.timeout = 10  # Set default timeout

    try:
        login_page = session.get("https://www.onlinejobs.ph/login", timeout=10)
        soup = BeautifulSoup(login_page.text, "lxml")
        csrf = soup.find("input", {"name": "csrf-token"})
        if not csrf:
            print("Could not find CSRF token")
            return None

        payload = {
            "csrf-token": csrf["value"],
            "info[email]": os.getenv("OJ_EMAIL"),
            "info[password]": os.getenv("OJ_PASSWORD"),
            "login": "Login →",
        }
        print("Attempting login...")

        auth_response = session.post(
            "https://www.onlinejobs.ph/authenticate",
            data=payload,
            headers={
                **HEADERS,
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://www.onlinejobs.ph",
                "referer": "https://www.onlinejobs.ph/login",
            },
            allow_redirects=True,
            timeout=10,
        )

        if "jobseekers" in auth_response.url.lower():
            print("Login successful")
            return session
        else:
            print(f"Login failed. Status: {auth_response.status_code}")
            print(f"Response URL: {auth_response.url}")
            return None
    except (requests.Timeout, requests.ConnectionError) as e:
        print(f"Error creating session: {e}")
        return None


def parse_jobs(html: str, keyword: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs = []

    for link in soup.select("a[href*='/jobseekers/job/']"):
        box = link.find("div", class_="jobpost-cat-box")
        if not box:
            continue

        title_tag = box.find("h4")
        title = title_tag.get_text(strip=True) if title_tag else ""
        badge = title_tag.find("span", class_="badge") if title_tag else None
        employment_type = badge.get_text(strip=True) if badge else ""
        if badge:
            title = title.replace(employment_type, "").strip()

        job_url = "https://www.onlinejobs.ph" + link["href"]
        job_id = link["href"].split("/")[-1]

        date_tag = box.find("p", class_="fs-13")
        posted_date = ""
        if date_tag:
            em = date_tag.find("em")
            posted_date = (
                em.get_text(strip=True).replace("Posted on ", "") if em else ""
            )

        salary_tag = box.find("dd")
        salary = salary_tag.get_text(strip=True) if salary_tag else ""

        tags = [
            a.get_text(strip=True)
            for a in box.select("div.job-tag a")
            if a.get_text(strip=True)
        ]

        jobs.append(
            {
                "job_id": job_id,
                "title": title,
                "employment_type": employment_type,
                "salary": salary,
                "posted_date": posted_date,
                "job_url": job_url,
                "tags": tags,
                "keyword": keyword,
            }
        )

    return jobs


def get_total_jobs(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    count_tag = soup.find("p", class_="fs-12")
    if count_tag:
        text = count_tag.get_text(strip=True)
        # "Displaying 30 out of 282 jobs"
        try:
            return int(text.split("out of")[1].split("jobs")[0].strip())
        except:
            pass
    return 0


def make_request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    refresh_session_fn: Callable[[], requests.Session | None],
    **kwargs,
) -> requests.Response | None:
    """Make a request with automatic session refresh on timeout/failure.

    Args:
        session: The current session
        method: HTTP method (get, post, etc.)
        url: URL to request
        refresh_session_fn: Function to call to refresh the session
        **kwargs: Additional arguments to pass to the request

    Returns:
        Response object or None if all retries failed
    """
    max_retries = 2
    retry_count = 0

    while retry_count < max_retries:
        try:
            request_fn = getattr(session, method.lower())
            response = request_fn(url, timeout=10, **kwargs)
            response.raise_for_status()  # Raise exception for bad status codes
            return response
        except (
            requests.Timeout,
            requests.ConnectionError,
            requests.RequestException,
        ) as e:
            retry_count += 1
            print(f"  Request failed ({e.__class__.__name__}): {e}")

            if retry_count < max_retries:
                print(
                    f"  Refreshing session and retrying... (attempt {retry_count}/{max_retries})"
                )
                new_session = refresh_session_fn()
                if new_session:
                    session = new_session
                else:
                    print("  Failed to refresh session")
                    return None
            else:
                print(f"  Max retries ({max_retries}) exceeded")
                return None

    return None


def search_jobs(
    session: requests.Session, keyword: str, max_pages: int = 2
) -> list[dict]:
    print(f"Searching: '{keyword}'")

    params = {
        "jobkeyword": keyword,
        "skill_tags": "",
        "gig": "on",
        "partTime": "on",
        "fullTime": "on",
        "isFromJobsearchForm": "1",
    }

    response = make_request_with_retry(
        session,
        "get",
        "https://www.onlinejobs.ph/jobseekers/jobsearch",
        create_session,
        params=params,
        headers={**HEADERS, "referer": "https://www.onlinejobs.ph/jobs"},
    )

    if not response:
        print(f"  Failed to fetch search results for '{keyword}'")
        return []

    all_jobs = parse_jobs(response.text, keyword)
    total = get_total_jobs(response.text)
    total_pages = min(max_pages, -(-total // 30))
    print(
        f"  Found {total} jobs across ~{total_pages} pages (fetching up to {max_pages})"
    )

    for page in range(1, total_pages):
        offset = page * 30
        page_response = make_request_with_retry(
            session,
            "get",
            f"https://www.onlinejobs.ph/jobseekers/jobsearch/{offset}",
            create_session,
            params=params,
            headers={
                **HEADERS,
                "referer": "https://www.onlinejobs.ph/jobseekers/jobsearch",
            },
        )

        if not page_response:
            print(f"  Failed to fetch page {page + 1}, stopping pagination")
            break

        page_jobs = parse_jobs(page_response.text, keyword)
        all_jobs.extend(page_jobs)
        print(f"  Page {page + 1}: +{len(page_jobs)} jobs")

    return all_jobs


if __name__ == "__main__":
    keywords = ["automation", "n8n", "python", "data engineer", "web scraping"]

    session = create_session()
    if not session:
        exit()

    all_results = []
    for keyword in keywords:
        jobs = search_jobs(session, keyword, max_pages=2)
        all_results.extend(jobs)

    # Deduplicate by job_id
    seen = set()
    unique_jobs = []
    for job in all_results:
        if job["job_id"] not in seen:
            seen.add(job["job_id"])
            unique_jobs.append(job)

    print(f"\nTotal unique jobs: {len(unique_jobs)}")
    print(json.dumps(unique_jobs[:2], indent=2))

    with open("jobs_output.json", "w", encoding="utf-8") as f:
        json.dump(unique_jobs, f, indent=2, ensure_ascii=False)
    print("Saved to jobs_output.json")
