"""本地 Skill 发现、加载和访问异常。"""

from matterloop_tools.errors import ToolError


class SkillError(ToolError):
    """所有 Skill 子系统异常的基类。"""


class SkillConfigurationError(SkillError):
    """Skill 加载器或访问策略配置不安全。"""


class SkillNameError(SkillError, ValueError):
    """Skill 名称不符合受限命名规则。"""


class SkillPathError(SkillError):
    """Skill 路径逃逸、包含符号链接或文件类型非法。"""


class SkillFormatError(SkillError):
    """SKILL.md 编码、frontmatter 或正文格式非法。"""


class SkillTooLargeError(SkillError):
    """Skill 文件或待注入内容超过配置上限。"""


class SkillNotFoundError(SkillError):
    """请求的 Skill 不存在。"""

    def __init__(self, name: str) -> None:
        """初始化未找到异常。

        Args:
            name: 未找到的 Skill 名称。
        """
        super().__init__(f"skill not found: {name}")


class SkillExistsError(SkillError):
    """注册表中已存在同名 Skill。"""

    def __init__(self, name: str) -> None:
        """初始化名称冲突异常。

        Args:
            name: 已存在的 Skill 名称。
        """
        super().__init__(f"skill already exists: {name}")


class SkillAccessDeniedError(SkillError):
    """访问策略未允许当前 Skill。"""

    def __init__(self, name: str) -> None:
        """初始化访问拒绝异常。

        Args:
            name: 被拒绝的 Skill 名称。
        """
        super().__init__(f"skill access denied: {name}")
