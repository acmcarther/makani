#!/usr/bin/python
# Copyright 2020 Makani Technologies LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Generate the routes.[ch] files."""

import os
import re
import sys
import textwrap

from makani.avionics.network import network_config
from makani.avionics.network import network_util


class SwitchMismatchException(Exception):
  pass


def _EnforceMatchingSwitches(switch_a, switch_b):
  """Enforces that the configurations for a and b network core switches match.

  Args:
    switch_a: cs_a or cs_gs_a.
    switch_b: cs_b or cs_gs_b, as appropriate to switch_a.
  Raises:
    SwitchMismatchException: if there's anything significantly different between
        the switch configurations.
  """

  if switch_a.get('asymmetric_ports') != switch_b.get('asymmetric_ports'):
    raise SwitchMismatchException('Different asymmetric ports on a/b switches')
  for port, connection in switch_a['ports'].iteritems():
    if not connection:
      expected = None
    elif connection.endswith('.0'):
      expected = connection[:-1] + '1'
    elif connection.endswith('_a'):
      expected = connection[:-1] + 'b'
    elif re.match(r'.*_a\.', connection):
      expected = re.sub(r'(.*)_a\.', r'\1_b.', connection)
    else:
      raise SwitchMismatchException('Unrecognized connection format: "%s".' %
                                    connection)
    other = switch_b['ports'].get(port)
    if other != expected and port not in switch_a.get('asymmetric_ports'):
      raise SwitchMismatchException('Connection mismatch: %s vs %s.' %
                                    (connection, other))


def _WriteForwardingMapHeader(autogen_root, output_dir, script_name, switches):
  """Writes the header file for the forwarding map C module.

  Args:
    autogen_root: The MAKANI_HOME-equivalent top directory.
    output_dir: The directory in which to output the files.
    script_name: This script's filename.
    switches: The 'switches' field of the yaml file.
  Raises:
    SwitchMismatchException: if there's anything obviously wrong with the
        configurations of the core switches.
  """

  file_name = os.path.join(output_dir, 'routes.h')
  rel_path = os.path.relpath(file_name, autogen_root)
  header_guard = re.sub('[/.]', '_', rel_path.upper()) + '_'

  for switch_location in ['wing', 'gs']:
    if switch_location == 'gs':
      switch = switches['cs_gs_a']
      _EnforceMatchingSwitches(switch, switches['cs_gs_b'])
    else:
      switch = switches['cs_a']
      _EnforceMatchingSwitches(switch, switches['cs_b'])

  body = textwrap.dedent("""
      #ifndef {guard}
      #define {guard}

      // Generated by {name}; do not edit.

      #include <stdint.h>

      #include "avionics/network/aio_node.h"

      // Returns pointer to multicast forwarding table indexed by MessageType.
      // TODO: Restructure forwarding map into const struct pointed to
      // by SwitchInfo.
      const uint32_t *AioMessageForwardingMap(AioNode node, int32_t *size);
      const uint32_t *EopMessageForwardingMap(AioNode node, int32_t *size);
      const uint32_t *WinchMessageForwardingMap(AioNode node, int32_t *size);

      #endif  // {guard}
      """[1:]).format(guard=header_guard, name=script_name)

  with open(file_name, 'w') as f:
    f.write(body)


def _GetForwardingMapDeclarations(type_name, enum_prefix, forwarding_maps,
                                  message_types, config):
  """Get forwarding map declarations for either winch or AIO messages.

  Args:
    type_name: Message type name (e.g., 'Aio' or 'Winch').
    enum_prefix: Message type enum prefix (e.g., MessageType or
                 WinchMessageType).
    forwarding_maps: A dictionary of forwarding maps returned by
                     MakeForwardingMaps.
    message_types: A sorted list of message types.
    config: A NetworkConfig.
  Returns:
    Each declaration inside a forwarding map, as a list of strings to be joined
        by newlines.
  """
  ret = []
  for node in config.aio_nodes:
    if not node.tms570_node:
      continue
    if (type_name == 'Eop' and not node.camel_name.startswith('Eop')
        and not node.camel_name.startswith('Cs')):
      continue
    if (type_name == 'Winch' and not node.camel_name.startswith('Plc')
        and not node.camel_name.startswith('CsGs')):
      continue
    table = forwarding_maps[network_util.GetAttachedSwitch(
        node, config.GetSwitches())]
    ret.append('static const uint32_t '
               'k{0}ForwardingMap{1}[kNum{2}s] = {{'.format(
                   type_name, node.camel_name, enum_prefix))
    entries = []
    for message_type in message_types:
      out_ports = table[message_type.name]
      entries.append(('  [%s] ' % message_type.enum_name,
                      '= %#09x,' % out_ports))

    max_length = max(len(x[0]) for x in entries)
    for entry in entries:
      ret.append(entry[0].ljust(max_length, ' ') + entry[1])
    ret.append('};')

  return ret


def _GetForwardingMap(forwarding_maps, message_types, config):
  """Get a forwarding map for either winch or AIO messages.

  Args:
    forwarding_maps: A forwarding map returned by MakeForwardingMaps.
    message_types: A sorted list of message types.
    config: A NetworkConfig.
  Returns:
    The forwarding map in C, as a list of strings to be joined by newlines.
  """
  assert len(set([m.type_name for m in message_types])) == 1
  type_name = message_types[0].type_name

  assert len(set([m.enum_prefix for m in message_types])) == 1
  enum_prefix = message_types[0].enum_prefix

  entries = _GetForwardingMapDeclarations(type_name, enum_prefix,
                                          forwarding_maps, message_types,
                                          config)
  template = textwrap.dedent("""
      const uint32_t *{0}MessageForwardingMap(AioNode aio_node,
                                              {2}int32_t *size) {{
        assert(size != NULL);
        *size = kNum{1}s;
        switch (aio_node) {{"""[1:])
  entries.append(template.format(type_name, enum_prefix, ' ' * len(type_name)))

  need_return = False
  for node in config.aio_nodes:
    if node.tms570_node:
      if need_return:
        need_return = False
        entries.append('      assert(false);')
        entries.append('      return NULL;')
      entries.append('    case %s:' % node.enum_name)
      if (type_name == 'Aio'
          or (type_name == 'Eop' and node.camel_name.startswith('Cs'))
          or (type_name == 'Winch' and node.camel_name.startswith('CsGs'))
          or (type_name == 'Winch' and node.camel_name.startswith('Plc'))):
        entries.append(
            '      return k%sForwardingMap%s;' % (type_name, node.camel_name))
      else:
        entries.append(
            '      return NULL;')
    else:
      entries.append('    case %s:' % node.enum_name)
      need_return = True

  entries.append(textwrap.dedent("""
          case kAioNodeForceSigned:
          case kNumAioNodes:
          default:
            assert(false);
            return NULL;
        }
      }
      """[1:]))
  return entries


def _WriteForwardingMapSource(autogen_root, output_dir, script_name,
                              config):
  """Writes the implementation file for the forwarding map C module.

  Args:
    autogen_root: The MAKANI_HOME-equivalent top directory.
    output_dir: The directory in which to output the files.
    script_name: This script's filename.
    config: A NetworkConfig.
  """

  file_basename = os.path.join(output_dir, 'routes')
  rel_path = os.path.relpath(file_basename, autogen_root)
  parts = [textwrap.dedent("""
      // Generated by {0}; do not edit.

      #include "{1}"
      """[1:]).format(script_name, rel_path + '.h')]

  parts.append(textwrap.dedent("""\
      #include <assert.h>
      #include <stddef.h>
      #include <stdint.h>

      #include "avionics/network/eop_message_type.h"
      #include "avionics/network/message_type.h"
      #include "avionics/network/winch_message_type.h"
      """))

  for messages in config.messages_by_type.values():
    path_finder = network_util.PathFinder(config.GetSwitches(), messages)
    forward = network_util.MakeForwardingMaps(messages, path_finder)
    parts.extend(_GetForwardingMap(forward, messages, config))

  with open(file_basename + '.c', 'w') as f:
    f.write('\n'.join(parts))


def main(argv):
  flags, argv = network_config.ParseGenerationFlags(argv)

  config = network_config.NetworkConfig(flags.network_file)

  script_name = os.path.basename(argv[0])
  _WriteForwardingMapHeader(flags.autogen_root, flags.output_dir, script_name,
                            config.GetSwitches())
  _WriteForwardingMapSource(flags.autogen_root, flags.output_dir, script_name,
                            config)

if __name__ == '__main__':
  main(sys.argv)