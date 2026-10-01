#!/usr/bin/python
"""Test module: returns a greeting, changes nothing."""

DOCUMENTATION = r"""
module: hello
short_description: Return a greeting (lamplighter test collection)
options:
  name:
    description: Who to greet.
    type: str
    required: true
author: lamplighter tests
"""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec={"name": {"type": "str", "required": True}})
    module.exit_json(changed=False, msg=f"hello {module.params['name']}")


if __name__ == "__main__":
    main()
