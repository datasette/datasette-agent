<script lang="ts">
  import type { IndexPageData } from "../../page_data/IndexPageData.types.ts";
  import { loadPageData } from "../../page_data/load.ts";

  const pageData = loadPageData<IndexPageData>();

  let message = $state("");
  let starting = $state(false);

  async function startChat(e: Event) {
    e.preventDefault();
    const trimmed = message.trim();
    if (!trimmed) return;
    starting = true;
    try {
      const resp = await fetch("/-/agent/api/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: trimmed }),
      });
      const data = await resp.json();
      sessionStorage.setItem("agent_pending_message", trimmed);
      window.location.href = "/-/agent/" + data.conversation_id;
    } catch (err) {
      starting = false;
      alert("Error: " + (err as Error).message);
    }
  }
</script>

<h1>Agent</h1>

<form onsubmit={startChat}>
  <div class="agent-new-chat">
    <textarea
      bind:value={message}
      placeholder="Ask a question about your data..."
      rows="3"
    ></textarea>
    <button type="submit" disabled={starting}>
      {starting ? "Starting..." : "Start chat"}
    </button>
  </div>
</form>

{#if pageData.conversations.length > 0}
  <h2>Previous conversations</h2>
  <ul class="agent-conversations">
    {#each pageData.conversations as conv}
      <li>
        <a href="/-/agent/{conv.id}">{conv.title ?? "Untitled conversation"}</a>
        <span class="agent-timestamp">{conv.updated_at}</span>
      </li>
    {/each}
  </ul>
{/if}
