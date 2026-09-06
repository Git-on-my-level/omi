import 'package:flutter/material.dart';

import 'package:omi/backend/schema/chat_content_block.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/message.dart';
import 'package:omi/pages/chat/widgets/markdown_message_widget.dart';
import 'package:omi/widgets/text_selection_controls.dart';

import 'agent_run_blocks.dart';
import 'conversation_link_blocks.dart';
import 'discovery_card_block.dart';
import 'goal_link_block.dart';
import 'memory_link_block.dart';
import 'question_card_block.dart';
import 'task_card_block.dart';

/// Renders the interactable components for a message's `content_blocks`.
///
/// Every block the desktop transcript draws as its own control has a component
/// here, so a turn reads the same on both clients. Thinking, toolCall, citation
/// and unknown types use their synthesized fallback line only when this list
/// replaces the body; with a normal body, that text is already covered there.
/// A text block is rendered here only when the body is the fallback projection:
/// that keeps prose from disappearing when the same projection also contains
/// an interactive block, without repeating prose that already has a normal
/// message body.
class ChatContentBlockList extends StatelessWidget {
  const ChatContentBlockList({
    super.key,
    required this.message,
    required this.sendMessage,
    this.onAskOmi,
    this.renderStructuredFallbackText = false,
    this.fetchConversation,
  });

  final ServerMessage message;
  final void Function(String) sendMessage;
  final Function(String)? onAskOmi;
  final bool renderStructuredFallbackText;
  final Future<ServerConversation?> Function(String id)? fetchConversation;

  /// True when at least one block in [message] has an interactable component.
  static bool hasRenderableBlocks(ServerMessage message) {
    return message.typedContentBlocks.any(_isRenderable);
  }

  static bool _isRenderable(ChatContentBlock block) {
    return block is TaskCardContentBlock ||
        block is GoalLinkContentBlock ||
        block is CaptureLinkContentBlock ||
        block is ConversationLinkContentBlock ||
        block is MemoryLinkContentBlock ||
        block is QuestionCardContentBlock ||
        block is DiscoveryCardContentBlock ||
        block is AgentSpawnContentBlock ||
        block is AgentCompletionContentBlock;
  }

  Widget? _build(ChatContentBlock block) {
    switch (block) {
      case TaskCardContentBlock():
        return TaskCardBlock(block: block);
      case GoalLinkContentBlock():
        return GoalLinkBlock(block: block);
      case CaptureLinkContentBlock():
        return CaptureLinkBlock(block: block, fetchConversation: fetchConversation);
      case ConversationLinkContentBlock():
        return ConversationLinkBlock(block: block, fetchConversation: fetchConversation);
      case MemoryLinkContentBlock():
        return MemoryLinkBlock(block: block);
      case QuestionCardContentBlock():
        return QuestionCardBlock(block: block, sendMessage: sendMessage);
      case DiscoveryCardContentBlock():
        return DiscoveryCardBlock(block: block);
      case AgentSpawnContentBlock():
        return AgentSpawnBlock(block: block);
      case AgentCompletionContentBlock():
        return AgentCompletionBlock(block: block);
      case TextContentBlock():
        if (renderStructuredFallbackText && block.text.trim().isNotEmpty) {
          return _StructuredFallbackText(
            key: ValueKey('chat-block-text-${block.id}'),
            text: block.text,
            onAskOmi: onAskOmi,
          );
        }
        return null;
      case ThinkingContentBlock():
      case ToolCallContentBlock():
      case CitationContentBlock():
      case UnknownContentBlock():
        return null;
    }
  }

  @override
  Widget build(BuildContext context) {
    final children = <Widget>[];
    for (final block in message.typedContentBlocks) {
      final widget = _build(block) ?? (renderStructuredFallbackText ? _fallbackTextWidget(block) : null);
      if (widget == null) continue;
      if (children.isNotEmpty) children.add(const SizedBox(height: 8));
      children.add(widget);
    }
    if (children.isEmpty) return const SizedBox.shrink();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: children,
    );
  }

  Widget? _fallbackTextWidget(ChatContentBlock block) {
    final fallback = message.structuredFallbackTextForBlock(block.id);
    if (fallback == null) return null;
    return _StructuredFallbackText(
      key: ValueKey('chat-block-fallback-${block.id}'),
      text: fallback,
      onAskOmi: onAskOmi,
    );
  }
}

class _StructuredFallbackText extends StatelessWidget {
  const _StructuredFallbackText({super.key, required this.text, this.onAskOmi});

  final String text;
  final Function(String)? onAskOmi;

  @override
  Widget build(BuildContext context) {
    String? selectedText;
    return SelectionArea(
      onSelectionChanged: (selectedContent) {
        selectedText = selectedContent?.plainText;
      },
      contextMenuBuilder: (context, selectableRegionState) {
        return omiSelectionMenuBuilder(
          context,
          selectableRegionState,
          (selected) => onAskOmi?.call(selected),
          selectedText: selectedText,
        );
      },
      child: getMarkdownWidget(context, text, onAskOmi: onAskOmi),
    );
  }
}
