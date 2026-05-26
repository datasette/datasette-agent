<script lang="ts">
  import * as smd from "../lib/smd.js";

  let contentEl: HTMLDivElement | undefined = $state();
  let parser: ReturnType<typeof smd.parser> | null = null;

  export function write(chunk: string) {
    if (!contentEl) return;
    if (!parser) {
      const renderer = smd.default_renderer(contentEl);
      parser = smd.parser(renderer);
    }
    smd.parser_write(parser, chunk);
  }

  export function end() {
    if (parser) {
      smd.parser_end(parser);
      parser = null;
    }
  }
</script>

<div class="agent-message agent-message-assistant">
  <div class="agent-message-content" bind:this={contentEl}></div>
</div>
