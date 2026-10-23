import 'package:flutter/material.dart';
import 'package:flutter_hbb/common.dart';
import 'package:flutter_hbb/models/platform_model.dart';
import 'package:settings_ui/settings_ui.dart';

const String kOptionThreeFingerScrollSpeed = "three-finger-scroll-speed";

double threeFingerScrollSpeed() {
  final v = double.tryParse(
      bind.mainGetLocalOption(key: kOptionThreeFingerScrollSpeed));
  if (v == null || v <= 0) return 1.0;
  return v > 10 ? 10 : v;
}

SettingsTile threeFingerScrollSpeedTile(
    void Function(void Function()) setState) {
  final speed = bind.mainGetLocalOption(key: kOptionThreeFingerScrollSpeed);
  return SettingsTile(
    title: Text(translate('Three-finger scroll speed')),
    value: Text('${speed.isEmpty ? '1.0' : speed}x'),
    onPressed: (context) async {
      final controller = TextEditingController(text: speed);
      await gFFI.dialogManager.show((setState, close, context) {
        submit() async {
          final v = double.tryParse(controller.text.trim());
          if (v == null || v < 0.1 || v > 10) return;
          await bind.mainSetLocalOption(
              key: kOptionThreeFingerScrollSpeed, value: v.toString());
          close();
        }

        return CustomAlertDialog(
          title: Text(translate('Three-finger scroll speed')),
          content: TextField(
            controller: controller,
            autofocus: true,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            decoration: const InputDecoration(hintText: '0.1 - 10'),
          ),
          actions: [
            dialogButton("Cancel", onPressed: close, isOutline: true),
            dialogButton("OK", onPressed: submit),
          ],
          onSubmit: submit,
          onCancel: close,
        );
      });
      setState(() {});
    },
  );
}
