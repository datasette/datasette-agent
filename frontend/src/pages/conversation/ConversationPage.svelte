<script lang="ts">
  import type { ConversationPageData } from "../../page_data/ConversationPageData.types.ts";
  import { loadPageData } from "../../page_data/load.ts";
  import { streamSSE } from "../../lib/sse.ts";
  import type { SSEEvent } from "../../lib/sse.ts";
  import ChatMessage from "../../components/ChatMessage.svelte";
  import ToolCallDetails from "../../components/ToolCallDetails.svelte";
  import ToolResultDetails from "../../components/ToolResultDetails.svelte";
  import StreamingMessage from "../../components/StreamingMessage.svelte";
  import MessageInput from "../../components/MessageInput.svelte";
  import * as smd from "../../lib/smd.js";

  const pageData = loadPageData<ConversationPageData>();

  // Message display list — a union of history + streaming items
  interface DisplayItem {
    type: "user" | "assistant" | "tool_call" | "tool_result" | "streaming";
    content?: string;
    toolName?: string;
    toolArguments?: string | Record<string, unknown>;
    toolOutput?: string;
    pending?: boolean;
  }

  let displayItems = $state<DisplayItem[]>(
    pageData.messages.map((m) => {
      if (m.role === "user") {
        return { type: "user" as const, content: m.content ?? "" };
      } else if (m.role === "assistant") {
        return { type: "assistant" as const, content: m.content ?? "" };
      } else if (m.role === "tool_call") {
        return {
          type: "tool_call" as const,
          toolName: m.tool_name ?? "",
          toolArguments: m.tool_arguments ?? "{}",
        };
      } else {
        return {
          type: "tool_result" as const,
          toolName: m.tool_name ?? "",
          toolOutput: m.tool_output ?? "",
        };
      }
    }),
  );

  let streaming = $state(false);
  let messagesEl: HTMLDivElement | undefined = $state();
  let streamingMessageComponent: StreamingMessage | undefined = $state();

  function scrollToBottom() {
    if (messagesEl) {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  }

  async function sendMessage(message: string) {
    streaming = true;

    // Add user message to display
    displayItems = [...displayItems, { type: "user", content: message }];
    await tick();
    scrollToBottom();

    // Add streaming placeholder
    displayItems = [...displayItems, { type: "streaming" }];
    await tick();
    scrollToBottom();

    let hasStreamingContent = false;

    try {
      for await (const event of streamSSE(pageData.conversation_id, message)) {
        if (event.type === "text_chunk") {
          if (!hasStreamingContent) {
            hasStreamingContent = true;
          }
          streamingMessageComponent?.write(event.content);
          scrollToBottom();
        } else if (event.type === "tool_call") {
          // End streaming message if active
          if (hasStreamingContent) {
            streamingMessageComponent?.end();
            // Replace streaming item with the rendered content
            const streamingEl = messagesEl?.querySelector(
              ".agent-message-assistant:last-of-type .agent-message-content",
            );
            const renderedHtml = streamingEl?.innerHTML ?? "";
            displayItems = displayItems.filter((d) => d.type !== "streaming");
            if (renderedHtml) {
              displayItems = [
                ...displayItems,
                { type: "assistant", content: "" },
              ];
              // We'll set innerHTML after tick
              await tick();
              const lastAssistant = messagesEl?.querySelectorAll(
                ".agent-message-assistant .agent-message-content",
              );
              if (lastAssistant && lastAssistant.length > 0) {
                lastAssistant[lastAssistant.length - 1]!.innerHTML =
                  renderedHtml;
              }
            }
            hasStreamingContent = false;
          }
          // Remove streaming placeholder if still present
          displayItems = displayItems.filter((d) => d.type !== "streaming");
          displayItems = [
            ...displayItems,
            {
              type: "tool_call",
              toolName: event.name,
              toolArguments: event.arguments,
              pending: true,
            },
          ];
          await tick();
          scrollToBottom();
        } else if (event.type === "tool_result") {
          // Mark matching pending tool call as not pending
          displayItems = displayItems.map((d) =>
            d.type === "tool_call" &&
            d.toolName === event.name &&
            d.pending
              ? { ...d, pending: false }
              : d,
          );
          displayItems = [
            ...displayItems,
            {
              type: "tool_result",
              toolName: event.name,
              toolOutput: event.output,
            },
          ];
          // Add new streaming placeholder for next text
          displayItems = [...displayItems, { type: "streaming" }];
          await tick();
          scrollToBottom();
        } else if (event.type === "done") {
          if (hasStreamingContent) {
            streamingMessageComponent?.end();
          }
          // Clean up streaming placeholder
          displayItems = displayItems.filter((d) => d.type !== "streaming");
        } else if (event.type === "error") {
          if (hasStreamingContent) {
            streamingMessageComponent?.end();
          }
          displayItems = displayItems.filter((d) => d.type !== "streaming");
          displayItems = [
            ...displayItems,
            { type: "assistant", content: "Error: " + event.message },
          ];
        }
      }
    } catch (err) {
      if (hasStreamingContent) {
        streamingMessageComponent?.end();
      }
      displayItems = displayItems.filter((d) => d.type !== "streaming");
      displayItems = [
        ...displayItems,
        {
          type: "assistant",
          content: "Connection error: " + (err as Error).message,
        },
      ];
    } finally {
      streaming = false;
      await tick();
      scrollToBottom();
    }
  }

  // Handle pending message from index page redirect
  import { tick } from "svelte";

  $effect(() => {
    const pending = sessionStorage.getItem("agent_pending_message");
    if (pending) {
      sessionStorage.removeItem("agent_pending_message");
      sendMessage(pending);
    }
  });
</script>

<div class="agent-chat">
  <div class="agent-header">
    <a href="/-/agent">&larr; Back</a>
    <h1>{pageData.title ?? "Chat"}</h1>
  </div>

  <div class="agent-messages" bind:this={messagesEl}>
    {#each displayItems as item}
      {#if item.type === "user"}
        <ChatMessage role="user" content={item.content ?? ""} />
      {:else if item.type === "assistant"}
        <ChatMessage role="assistant" content={item.content ?? ""} />
      {:else if item.type === "tool_call"}
        <ToolCallDetails
          name={item.toolName ?? ""}
          arguments={item.toolArguments ?? "{}"}
          pending={item.pending ?? false}
        />
      {:else if item.type === "tool_result"}
        <ToolResultDetails
          name={item.toolName ?? ""}
          output={item.toolOutput ?? ""}
        />
      {:else if item.type === "streaming"}
        <StreamingMessage bind:this={streamingMessageComponent} />
      {/if}
    {/each}
  </div>

  <MessageInput onSend={sendMessage} disabled={streaming} />
</div>
