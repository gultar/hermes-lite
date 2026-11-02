<!--
Hermes Lite prompt: tool_skill_manage
Description of the skill_manage tool.
To change it: copy this file to ~/.hermes-lite/prompts/tool_skill_manage.md and edit the copy.
-->
Create and maintain skills: reusable procedures, pitfalls and the user's conventions for a kind of task. Actions: create (name, content = full SKILL.md with YAML frontmatter name/description + markdown body, optional category), patch (name, old_string, new_string; optional file_path for a supporting file), write_file (name, file_path, file_content), remove_file (name, file_path), delete (name). Pass several changes as 'operations'.
