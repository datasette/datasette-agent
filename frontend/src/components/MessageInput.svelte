<script lang="ts">
  let {
    onSend,
    disabled = false,
  }: {
    onSend: (message: string) => void;
    disabled?: boolean;
  } = $props();

  let message = $state("");

  function handleSubmit(e: Event) {
    e.preventDefault();
    const trimmed = message.trim();
    if (!trimmed) return;
    onSend(trimmed);
    message = "";
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      handleSubmit(e);
    }
  }
</script>

<form class="agent-input" onsubmit={handleSubmit}>
  <textarea
    bind:value={message}
    placeholder="Type a message..."
    rows="2"
    {disabled}
    onkeydown={handleKeydown}
  ></textarea>
  <button type="submit" {disabled}>Send</button>
</form>
