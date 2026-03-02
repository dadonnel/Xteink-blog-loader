from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opml_store import OPMLStore, ValidationError


app = Flask(__name__)
store = OPMLStore(PROJECT_ROOT / "feeds.opml")


PAGE_TEMPLATE = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Feed Manager</title>
    <style>
      body { font-family: sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
      .error { background: #ffe5e5; border: 1px solid #b00; color: #600; padding: .75rem; margin-bottom: 1rem; }
      .success { background: #e9ffe9; border: 1px solid #2a7; color: #153; padding: .75rem; margin-bottom: 1rem; }
      form { margin: 1rem 0; }
      input { margin-right: .5rem; padding: .3rem .4rem; }
      button { padding: .3rem .7rem; }
      ul { padding-left: 1.2rem; }
      li { margin-bottom: .35rem; }
      .feed-row { display: flex; align-items: center; gap: .5rem; }
      .url { color: #555; font-size: .9rem; }
    </style>
  </head>
  <body>
    <h1>Feed Manager</h1>

    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    {% if success %}<div class="success">{{ success }}</div>{% endif %}

    <h2>Add Feed</h2>
    <form method="post" action="{{ url_for('add_feed') }}">
      <input name="name" placeholder="Feed Name" value="{{ form_data.get('name', '') }}" required />
      <input name="url" placeholder="https://example.com/feed.xml" value="{{ form_data.get('url', '') }}" required />
      <input name="category" placeholder="Category (optional)" value="{{ form_data.get('category', '') }}" />
      <button type="submit">Add Feed</button>
    </form>

    <h2>Current Feeds</h2>
    {% if not feeds %}
      <p>No feeds configured yet.</p>
    {% endif %}

    {% for category, feed_items in feeds.items() %}
      <h3>{{ category }}</h3>
      <ul>
        {% for feed in feed_items %}
        <li>
          <div class="feed-row">
            <strong>{{ feed.name }}</strong>
            <span class="url">{{ feed.url }}</span>
            <form method="post" action="{{ url_for('delete_feed') }}" style="display:inline">
              <input type="hidden" name="feed_id" value="{{ feed.feed_id }}" />
              <button type="submit">Delete</button>
            </form>
          </div>
        </li>
        {% endfor %}
      </ul>
    {% endfor %}
  </body>
</html>
"""


@app.get("/")
def index():
    error = request.args.get("error")
    success = request.args.get("success")
    form_data = {
        "name": request.args.get("name", ""),
        "url": request.args.get("url", ""),
        "category": request.args.get("category", ""),
    }
    feeds = store.parse_feeds()
    return render_template_string(
        PAGE_TEMPLATE,
        feeds=feeds,
        error=error,
        success=success,
        form_data=form_data,
    )


@app.post("/feeds")
def add_feed():
    name = request.form.get("name", "")
    url = request.form.get("url", "")
    category = request.form.get("category", "")

    if not name.strip() or not url.strip():
        return redirect(
            url_for(
                "index",
                error="Name and URL are required.",
                name=name,
                url=url,
                category=category,
            )
        )

    try:
        store.append_feed(name=name, url=url, category=category)
    except ValidationError as exc:
        return redirect(
            url_for(
                "index",
                error=str(exc),
                name=name,
                url=url,
                category=category,
            )
        )

    return redirect(url_for("index", success="Feed added successfully."))


@app.post("/feeds/delete")
def delete_feed():
    url = request.form.get("url", "")
    feed_id = request.form.get("feed_id", "")

    try:
        deleted = store.delete_feed(url=url, feed_id=feed_id)
    except ValidationError as exc:
        return redirect(url_for("index", error=str(exc)))

    if not deleted:
        return redirect(url_for("index", error="Feed not found."))
    return redirect(url_for("index", success="Feed removed successfully."))


if __name__ == "__main__":
    app.run(debug=True, port=5002)
