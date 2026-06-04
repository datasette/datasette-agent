<script lang="ts">
  import * as smd from "../lib/smd.js";

  let { role, content }: { role: string; content: string } = $props();

  let contentEl: HTMLDivElement | undefined = $state();

  $effect(() => {
    if (contentEl && role === "assistant" && content) {
      contentEl.textContent = "";
      const renderer = smd.default_renderer(contentEl);
      const parser = smd.parser(renderer);
      smd.parser_write(parser, content);
      smd.parser_end(parser);
    }
  });
</script>

<div class="agent-message agent-message-{role}">
  <div class="agent-message-content" bind:this={contentEl}>
    {#if role !== "assistant"}
      {content}
    {/if}
  </div>
</div>
