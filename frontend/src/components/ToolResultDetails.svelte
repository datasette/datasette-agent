<script lang="ts">
  let { name, output }: { name: string; output: string } = $props();

  function prettyJson(text: string): string {
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      return text;
    }
  }

  function extractHtml(text: string): string {
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object" && parsed._html) {
        return parsed._html;
      }
    } catch {
      // not JSON
    }
    return "";
  }

  const htmlContent = $derived(extractHtml(output));
  const formatted = $derived(prettyJson(output));
</script>

{#if htmlContent}
  <div class="agent-rich-result">{@html htmlContent}</div>
{/if}
<details class="agent-tool-result">
  <summary>Result: {name}</summary>
  <pre>{formatted}</pre>
</details>
